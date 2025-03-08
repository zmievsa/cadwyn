import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from copy import deepcopy
from enum import Enum
from pathlib import Path
from typing import Any, Union

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pytest_fixture_classes import fixture_class

from cadwyn import Cadwyn, VersionBundle, VersionedAPIRouter
from cadwyn._utils import same_definition_as_in
from cadwyn.schema_generation import SchemaGenerator, generate_versioned_models
from cadwyn.structure import Version, VersionChange
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.schemas import AlterSchemaSubInstruction, SchemaHadInstruction
from cadwyn.structure.versions import HeadVersion

CURRENT_DIR = Path(__file__).parent
Undefined = object()


@pytest.fixture
def api_version_var():
    api_version_var = ContextVar("api_version_var")
    api_version_var.set(None)
    return api_version_var


@pytest.fixture
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
        api_version_var: Union[ContextVar, None] = None,
        api_version: Union[str, None, object] = Undefined,
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
    api_version_var: Union[ContextVar[Union[str, None]], None] = None,
):
    app = FastAPI()
    app.include_router(router)

    return TestClientWithHardcodedAPIVersion(app, api_version=api_version, api_version_var=api_version_var)


@pytest.fixture
def router() -> VersionedAPIRouter:
    return VersionedAPIRouter()


@fixture_class(name="create_runtime_schemas")
class CreateRuntimeSchemas:
    def __call__(self, *version_changes: type[VersionChange]) -> dict[str, SchemaGenerator]:
        return generate_versioned_models(VersionBundle(*versions(*version_changes)))


@fixture_class(name="create_versioned_app")
class CreateVersionedApp:
    api_version_var: ContextVar[Union[str, None]]
    router: VersionedAPIRouter

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: Union[VersionedAPIRouter, None] = None,
    ) -> Cadwyn:
        router = router or self.router
        app = Cadwyn(
            versions=VersionBundle(
                HeadVersion(*head_version_changes),
                *versions(*version_changes),
                api_version_var=self.api_version_var,
            )
        )
        app.generate_and_include_versioned_routers(router)
        app._cadwyn_initialize()
        return app


def versions(*version_changes: type[VersionChange]) -> list[Version]:
    versions = [Version("2000-01-01")]
    for i, change in enumerate(version_changes):
        versions.append(Version(f"{2001 + i}-01-01", change))
    return list(reversed(versions))


@fixture_class(name="create_versioned_clients")
class CreateVersionedClients:
    create_versioned_app: CreateVersionedApp
    api_version_var: ContextVar[Union[str, None]]

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: Union[VersionedAPIRouter, None] = None,
    ) -> dict[str, CadwynTestClient]:
        app = self.create_versioned_app(*version_changes, head_version_changes=head_version_changes, router=router)
        return {
            version: CadwynTestClient(app, headers={app.router.api_version_parameter_name: version})
            for version in reversed(app.router.versioned_routers)
        }


def version_change(
    *instructions: Union[
        SchemaHadInstruction, AlterSchemaSubInstruction, AlterEndpointSubInstruction, AlterEnumSubInstruction
    ],
    **body_items: Any,
) -> type[VersionChange]:
    return type(VersionChange)(
        "MyVersionChange",  # pyright: ignore
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
    if "model_name" in schema_to_modify["schema"]:
        del schema_to_modify["schema"]["model_name"]
    if "schema" in schema_to_modify["schema"]:
        del schema_to_modify["schema"]["schema"]["model_name"]
    del schema_to_modify["config"]["title"]
    return serialize_object(schema)


def serialize_object(obj: Any):
    if isinstance(obj, dict):
        obj.pop("schema_ref", None)
        obj.pop("ref", None)
        return {k: v.__name__ if callable(v) else serialize_object(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        modified_list = []
        for v in obj:
            if callable(v):
                v = v.__name__  # noqa: PLW2901
            modified_list.append(serialize_object(v))
        return modified_list
    else:
        return obj
