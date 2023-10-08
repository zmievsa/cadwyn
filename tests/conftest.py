import importlib
import random
import shutil
import string
import uuid
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pytest_fixture_classes import fixture_class
from typing_extensions import deprecated

from cadwyn import VersionBundle, VersionedAPIRouter, generate_code_for_versioned_packages, generate_versioned_routers
from cadwyn.codegen import _get_version_dir_name
from cadwyn.structure import Version, VersionChange
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.schemas import AlterSchemaSubInstruction

CURRENT_DIR = Path(__file__).parent
Undefined = object()


@pytest.fixture()
def api_version_var():
    api_version_var = ContextVar("api_version_var")
    api_version_var.set(None)
    return api_version_var


@pytest.fixture(scope="session")
def temp_dir():
    temp_dir = CURRENT_DIR / "_temp"
    temp_dir.mkdir(exist_ok=True)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)


@pytest.fixture()
def data_dir(temp_dir: Path):
    random_dir_name = "".join(random.choices(string.ascii_letters, k=10))
    random_dir = temp_dir / random_dir_name
    shutil.copytree(CURRENT_DIR / "_data", CURRENT_DIR / random_dir)
    try:
        yield random_dir
    finally:
        shutil.rmtree(random_dir)


@pytest.fixture()
def data_package_path(temp_dir: Path, data_dir: Path) -> str:
    return f"tests.{temp_dir.name}.{data_dir.name}"


@pytest.fixture()
def latest_module_path(data_package_path: str) -> str:
    return f"{data_package_path}.latest"


@pytest.fixture()
def latest_module(latest_module_path: str) -> ModuleType:
    return importlib.import_module(latest_module_path)


@pytest.fixture()
def random_uuid():
    return uuid.uuid4()


@fixture_class(name="run_schema_codegen")
class RunSchemaCodegen:
    data_package_path: str

    def __call__(self, versions: VersionBundle) -> Any:
        latest_module = importlib.import_module(self.data_package_path + ".latest")
        generate_code_for_versioned_packages(latest_module, versions)
        return latest_module


@fixture_class(name="create_versioned_schemas")
class CreateVersionedSchemas:
    api_version_var: ContextVar[date | None]
    data_package_path: str

    def __call__(self, *version_changes: type[VersionChange] | list[type[VersionChange]]) -> tuple[ModuleType, ...]:
        created_versions = versions(version_changes)
        generate_code_for_versioned_packages(
            importlib.import_module(self.data_package_path + ".latest"),
            VersionBundle(
                *created_versions,
                api_version_var=self.api_version_var,
            ),
        )

        return tuple(
            reversed(
                [
                    importlib.import_module(
                        self.data_package_path + f".{_get_version_dir_name(version.value)}",
                    )
                    for version in created_versions
                ],
            ),
        )


# TODO: Get rid of this class
@deprecated("Use create_versioned_schemas instead")
@fixture_class(name="create_simple_versioned_schemas")
class CreateSimpleVersionedSchemas:
    api_version_var: ContextVar[date | None]
    data_package_path: str
    create_versioned_schemas: CreateVersionedSchemas

    def __call__(self, *instructions: Any) -> tuple[ModuleType, ...]:
        return self.create_versioned_schemas(version_change(*instructions))


class TestClientWithAPIVersion(TestClient):
    def __init__(self, *args, **kwargs):
        self.api_version_var = kwargs.pop("api_version_var", None)
        self.api_version = kwargs.pop("api_version", Undefined)
        super().__init__(*args, **kwargs)
        self.app: FastAPI

    def request(self, *args, **kwargs):
        if self.api_version is not Undefined and self.api_version_var:
            self.api_version_var.set(self.api_version)
        return super().request(*args, **kwargs)


def client(
    router: APIRouter,
    api_version: Any = Undefined,
    api_version_var: ContextVar[date | None] | None = None,
) -> TestClientWithAPIVersion:
    app = FastAPI()
    app.include_router(router)

    return TestClientWithAPIVersion(app, api_version=api_version, api_version_var=api_version_var)


@pytest.fixture()
def router() -> VersionedAPIRouter:
    return VersionedAPIRouter()


@fixture_class(name="create_versioned_routers")
class CreateVersionedRouters:
    api_version_var: ContextVar[date | None]
    router: VersionedAPIRouter
    data_package_path: str
    run_schema_codegen: RunSchemaCodegen

    def __call__(self, *version_changes: type[VersionChange] | list[type[VersionChange]]) -> dict[date, APIRouter]:
        bundle = VersionBundle(*versions(version_changes), api_version_var=self.api_version_var)
        latest_module = self.run_schema_codegen(bundle)

        return generate_versioned_routers(
            self.router,
            versions=bundle,
            latest_schemas_module=latest_module,
        )


def versions(version_changes):
    versions = [Version(date(2000, 1, 1))]
    for i, change in enumerate(version_changes):
        if isinstance(change, list):
            versions.append(Version(date(2001 + i, 1, 1), *change))
        else:
            versions.append(Version(date(2001 + i, 1, 1), change))
    return list(reversed(versions))


@fixture_class(name="create_versioned_clients")
class CreateVersionedClients:
    create_versioned_routers: CreateVersionedRouters
    api_version_var: ContextVar[date | None]

    def __call__(
        self,
        *version_changes: type[VersionChange] | list[type[VersionChange]],
    ) -> dict[date, TestClientWithAPIVersion]:
        return {
            version: client(router, api_version=version, api_version_var=self.api_version_var)
            for version, router in self.create_versioned_routers(*version_changes).items()
        }


def version_change(
    *instructions: AlterSchemaSubInstruction | AlterEndpointSubInstruction | AlterEnumSubInstruction,
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
