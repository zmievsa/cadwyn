import importlib
import random
import shutil
import string
import textwrap
import uuid
from contextvars import ContextVar
from datetime import date
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pytest_fixture_classes import fixture_class

from cadwyn import VersionBundle, VersionedAPIRouter, generate_code_for_versioned_packages
from cadwyn._utils import same_definition_as_in
from cadwyn.codegen import _get_version_dir_name
from cadwyn.main import Cadwyn
from cadwyn.structure import Version, VersionChange
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.schemas import AlterSchemaInstruction, AlterSchemaSubInstruction

CURRENT_DIR = Path(__file__).parent
Undefined = object()


@pytest.fixture()
def api_version_var():
    api_version_var = ContextVar("api_version_var")
    api_version_var.set(None)
    return api_version_var


@pytest.fixture()
def temp_dir():
    temp_dir = CURRENT_DIR / "_data/_temp"
    temp_dir.mkdir(exist_ok=True)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)


@pytest.fixture()
def random_uuid():
    return uuid.uuid4()


@pytest.fixture()
def created_modules():
    # This a really-really bad pattern! I am very lazy and evil for doing this
    return []


@fixture_class(name="run_schema_codegen")
class RunSchemaCodegen:
    temp_data_package_path: str

    def __call__(self, versions: VersionBundle) -> Any:
        latest_module = importlib.import_module(self.temp_data_package_path + ".latest")
        generate_code_for_versioned_packages(latest_module, versions)
        return latest_module


@fixture_class(name="create_versioned_schemas")
class CreateVersionedSchemas:
    api_version_var: ContextVar[date | None]
    temp_data_package_path: str

    def __call__(
        self,
        *version_changes: type[VersionChange] | list[type[VersionChange]],
        ignore_coverage_for_latest_aliases: bool = True,
    ) -> tuple[ModuleType, ...]:
        created_versions = versions(version_changes)
        latest = importlib.import_module(self.temp_data_package_path + ".latest")
        generate_code_for_versioned_packages(
            latest,
            VersionBundle(
                *created_versions,
                api_version_var=self.api_version_var,
            ),
            ignore_coverage_for_latest_aliases=ignore_coverage_for_latest_aliases,
        )
        importlib.invalidate_caches()
        schemas = tuple(
            reversed(
                [
                    importlib.import_module(
                        self.temp_data_package_path + f".{_get_version_dir_name(version.value)}",
                    )
                    for version in created_versions
                ],
            ),
        )
        assert {k: v for k, v in schemas[-1].__dict__.items() if not k.startswith("__")} == {
            k: v for k, v in latest.__dict__.items() if not k.startswith("__")
        }
        return schemas


@pytest.fixture()
def temp_data_dir(temp_dir: Path) -> Path:
    data_dir_name = "".join(random.choices(string.ascii_letters, k=15))
    data_dir = temp_dir / data_dir_name
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def data_package_path() -> str:
    return "tests._data"


@pytest.fixture()
def temp_data_package_path(data_package_path: str, temp_dir: Path, temp_data_dir: Path) -> str:
    return f"{data_package_path}.{temp_dir.name}.{temp_data_dir.name}"


@pytest.fixture()
def latest_dir(temp_data_dir: Path):
    latest = temp_data_dir.joinpath("latest")
    latest.mkdir(parents=True)
    return latest


@pytest.fixture()
def latest_package_path(latest_dir: Path, temp_data_package_path: str) -> str:
    return f"{temp_data_package_path}.{latest_dir.name}"


@fixture_class(name="latest_module_for")
class LatestModuleFor:
    temp_dir: Path
    latest_dir: Path
    latest_package_path: str
    created_modules: list[ModuleType]

    def __call__(self, source: str) -> Any:
        source = textwrap.dedent(source).strip()
        self.latest_dir.joinpath("__init__.py").write_text(source)
        importlib.invalidate_caches()
        latest = importlib.import_module(self.latest_package_path)
        if self.created_modules:
            raise NotImplementedError("You cannot write latest twice")
        self.created_modules.append(latest)
        return latest


class _FakeModuleWithEmptyClasses:
    EmptyEnum: type[Enum]
    EmptySchema: type[BaseModel]


@pytest.fixture()
def latest_with_empty_classes(latest_module_for: LatestModuleFor) -> _FakeModuleWithEmptyClasses:
    return latest_module_for(
        """
        from enum import Enum, auto
        import pydantic

        class EmptyEnum(Enum):
            pass

        class EmptySchema(pydantic.BaseModel):
            pass
        """,
    )


@fixture_class(name="create_simple_versioned_schemas")
class CreateSimpleVersionedSchemas:
    api_version_var: ContextVar[date | None]
    temp_data_package_path: str
    create_versioned_schemas: CreateVersionedSchemas

    def __call__(self, *instructions: Any, ignore_coverage_for_latest_aliases: bool = True) -> tuple[ModuleType, ...]:
        return self.create_versioned_schemas(
            version_change(*instructions),
            ignore_coverage_for_latest_aliases=ignore_coverage_for_latest_aliases,
        )


@fixture_class(name="create_local_versioned_schemas")
class CreateLocalVersionedSchemas:
    api_version_var: ContextVar[date | None]
    temp_dir: Path
    created_modules: list[ModuleType]
    latest_package_path: str

    def __call__(
        self,
        *version_changes: type[VersionChange] | list[type[VersionChange]],
        ignore_coverage_for_latest_aliases: bool = True,
    ) -> tuple[ModuleType, ...]:
        latest = self.created_modules[0]
        created_versions = versions(version_changes)

        generate_code_for_versioned_packages(
            importlib.import_module(self.latest_package_path),
            VersionBundle(
                *created_versions,
                api_version_var=self.api_version_var,
            ),
            ignore_coverage_for_latest_aliases=ignore_coverage_for_latest_aliases,
        )
        importlib.invalidate_caches()

        schemas = tuple(
            reversed(
                [
                    importlib.import_module(
                        self.latest_package_path.removesuffix("latest") + f"{_get_version_dir_name(version.value)}",
                    )
                    for version in created_versions
                ],
            ),
        )

        # Validate that latest version is always equivalent to the template version
        assert {k: v for k, v in schemas[-1].__dict__.items() if not k.startswith("__")} == {
            k: v for k, v in latest.__dict__.items() if not k.startswith("__")
        }
        return schemas


@fixture_class(name="create_local_simple_versioned_schemas")
class CreateLocalSimpleVersionedSchemas:
    api_version_var: ContextVar[date | None]
    create_local_versioned_schemas: CreateLocalVersionedSchemas

    def __call__(self, *instructions: Any, ignore_coverage_for_latest_aliases: bool = True) -> ModuleType:
        return self.create_local_versioned_schemas(
            version_change(*instructions),
            ignore_coverage_for_latest_aliases=ignore_coverage_for_latest_aliases,
        )[0]


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


@fixture_class(name="create_versioned_app")
class CreateVersionedApp:
    api_version_var: ContextVar[date | None]
    router: VersionedAPIRouter
    temp_data_package_path: str
    run_schema_codegen: RunSchemaCodegen

    def __call__(self, *version_changes: type[VersionChange] | list[type[VersionChange]]) -> Cadwyn:
        bundle = VersionBundle(*versions(version_changes), api_version_var=self.api_version_var)
        latest_module = self.run_schema_codegen(bundle)
        app = Cadwyn(versions=bundle, latest_schemas_module=latest_module)
        app.generate_and_include_versioned_routers(self.router)
        return app


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
    create_versioned_app: CreateVersionedApp
    api_version_var: ContextVar[date | None]

    def __call__(
        self,
        *version_changes: type[VersionChange] | list[type[VersionChange]],
    ) -> dict[date, CadwynTestClient]:
        app = self.create_versioned_app(*version_changes)
        return {
            version: CadwynTestClient(app, headers={app.router.api_version_header_name: version.isoformat()})
            for version in app.router.versioned_routes
        }


def version_change(
    *instructions: AlterSchemaInstruction
    | AlterSchemaSubInstruction
    | AlterEndpointSubInstruction
    | AlterEnumSubInstruction,
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


def serialize(enum: type[Enum]) -> dict[str, Any]:
    return {member.name: member.value for member in enum}
