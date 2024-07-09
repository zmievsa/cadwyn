import importlib
import textwrap
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from copy import deepcopy
from datetime import date
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pytest_fixture_classes import fixture_class

from cadwyn import Cadwyn, VersionBundle, VersionedAPIRouter
from cadwyn._package_utils import get_version_dir_name
from cadwyn._utils import same_definition_as_in
from cadwyn.schema_generation import _generate_versioned_models, _SchemaGenerator
from cadwyn.structure import Version, VersionChange
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.modules import AlterModuleInstruction
from cadwyn.structure.schemas import AlterSchemaSubInstruction, SchemaHadInstruction
from cadwyn.structure.versions import HeadVersion

CURRENT_DIR = Path(__file__).parent
Undefined = object()
_T_MODEL = TypeVar("_T_MODEL", bound=type[BaseModel | Enum])


@pytest.fixture()
def api_version_var():
    api_version_var = ContextVar("api_version_var")
    api_version_var.set(None)
    return api_version_var


@pytest.fixture()
def random_uuid():
    return uuid.uuid4()


class CadwynTestClient(TestClient):
    @same_definition_as_in(TestClient.__init__)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.app: Cadwyn


class TestClientWithHardcodedAPIVersion(CadwynTestClient):
    def __init__(
        self,
        *args,
        api_version_var: ContextVar | None = None,
        api_version: date | object = Undefined,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.api_version_var = api_version_var
        self.api_version = api_version

    def request(self, *args, **kwargs):
        if self.api_version is not Undefined and self.api_version_var:
            self.api_version_var.set(self.api_version)
        return super().request(*args, **kwargs)


def client(
    router: APIRouter,
    api_version: Any = Undefined,
    api_version_var: ContextVar[date | None] | None = None,
):
    app = FastAPI()
    app.include_router(router)

    return TestClientWithHardcodedAPIVersion(app, api_version=api_version, api_version_var=api_version_var)


@pytest.fixture()
def router() -> VersionedAPIRouter:
    return VersionedAPIRouter()


@fixture_class(name="create_runtime_schemas")
class CreateRuntimeSchemas:
    def __call__(self, *version_changes: type[VersionChange]) -> dict[str, _SchemaGenerator]:
        return _generate_versioned_models(VersionBundle(*versions(version_changes)))


@fixture_class(name="create_versioned_app")
class CreateVersionedApp:
    api_version_var: ContextVar[date | None]
    router: VersionedAPIRouter

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: VersionedAPIRouter | None = None,
    ) -> Cadwyn:
        router = router or self.router
        app = Cadwyn(
            versions=VersionBundle(
                HeadVersion(*head_version_changes),
                *versions(version_changes),
                api_version_var=self.api_version_var,
            )
        )
        app.generate_and_include_versioned_routers(router)
        return app


def versions(version_changes):
    versions = [Version(date(2000, 1, 1))]
    for i, change in enumerate(version_changes):
        versions.append(Version(date(2001 + i, 1, 1), change))
    return list(reversed(versions))


@fixture_class(name="create_versioned_clients")
class CreateVersionedClients:
    create_versioned_app: CreateVersionedApp
    api_version_var: ContextVar[date | None]

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: VersionedAPIRouter | None = None,
    ) -> dict[date, CadwynTestClient]:
        app = self.create_versioned_app(*version_changes, head_version_changes=head_version_changes, router=router)
        return {
            version: CadwynTestClient(app, headers={app.router.api_version_header_name: version.isoformat()})
            for version in reversed(app.router.versioned_routers)
        }


def version_change(
    *instructions: SchemaHadInstruction
    | AlterSchemaSubInstruction
    | AlterEndpointSubInstruction
    | AlterEnumSubInstruction
    | AlterModuleInstruction,
    **body_items: Any,
):
    return type(VersionChange)(
        "MyVersionChange",
        (VersionChange,),
        {
            "description": "",
            "instructions_to_migrate_to_previous_version": instructions,
            **body_items,
        },
    )


def serialize_enum(enum: type[Enum]) -> dict[str, Any]:
    return {member.name: member.value for member in enum}


def assert_models_are_equal(model1: type[BaseModel], model2: type[BaseModel]):
    model1_schema = serialize_schema(model1.__pydantic_core_schema__)
    model2_schema = serialize_schema(model2.__pydantic_core_schema__)
    assert model1_schema == model2_schema


def serialize_schema(schema: Any):
    schema = deepcopy(schema)
    if "cls" in schema:
        schema_to_modify = schema
    else:
        schema_to_modify = schema["schema"]
    del schema_to_modify["cls"]
    del schema_to_modify["ref"]
    if "model_name" in schema_to_modify["schema"]:
        del schema_to_modify["schema"]["model_name"]
    elif "schema" in schema_to_modify["schema"]:
        del schema_to_modify["schema"]["schema"]["model_name"]
    del schema_to_modify["config"]["title"]
    return serialize_object(schema)


def serialize_object(obj: Any):
    if isinstance(obj, dict):
        return {k: v.__name__ if callable(v) else serialize_object(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        modified_list = []
        for v in obj:
            if callable(v):
                while hasattr(v, "func"):
                    v = v.func
                v = v.__name__
            modified_list.append(serialize_object(v))
        return modified_list
    else:
        return obj
