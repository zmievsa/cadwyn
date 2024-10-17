import importlib
import random
import shutil
import string
import textwrap
import uuid
from collections.abc import Sequence
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

from cadwyn import VersionBundle, VersionedAPIRouter
from cadwyn._package_utils import get_version_dir_name
from cadwyn._utils import same_definition_as_in
from cadwyn.codegen import (
    DEFAULT_CODEGEN_MIGRATION_PLUGINS,
    DEFAULT_CODEGEN_PLUGINS,
)
from cadwyn.codegen._common import CodegenPlugin, MigrationPlugin
from cadwyn.codegen._main import generate_code_for_versioned_packages
from cadwyn.main import Cadwyn
from cadwyn.structure import Version, VersionChange
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.modules import AlterModuleInstruction
from cadwyn.structure.schemas import AlterSchemaSubInstruction, SchemaHadInstruction
from cadwyn.structure.versions import HeadVersion

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
        head_package = importlib.import_module(self.temp_data_package_path + ".head")
        versions.head_schemas_package = head_package
        generate_code_for_versioned_packages(versions.head_schemas_package, versions)
        return versions.head_schemas_package


@fixture_class(name="create_versioned_packages")
class CreateVersionedPackages:
    api_version_var: ContextVar[date | None]
    temp_data_package_path: str

    def __call__(
        self,
        *version_changes: type[VersionChange],
    ) -> tuple[ModuleType, ...]:
        created_versions = versions(version_changes)
        latest = importlib.import_module(self.temp_data_package_path + ".head")
        generate_code_for_versioned_packages(
            latest,
            VersionBundle(
                *created_versions,
                api_version_var=self.api_version_var,
            ),
        )
        return tuple(
            reversed(
                [
                    importlib.import_module(
                        self.temp_data_package_path + f".{get_version_dir_name(version.value)}",
                    )
                    for version in created_versions
                ],
            ),
        )


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
def head_dir(temp_data_dir: Path):
    head = temp_data_dir.joinpath("head")
    head.mkdir(parents=True)
    return head


@pytest.fixture()
def head_package_path(head_dir: Path, temp_data_package_path: str) -> str:
    return f"{temp_data_package_path}.{head_dir.name}"


@fixture_class(name="head_module_for")
class HeadModuleFor:
    temp_dir: Path
    head_dir: Path
    head_package_path: str
    created_modules: list[ModuleType]

    def __call__(self, source: str) -> Any:
        source = textwrap.dedent(source).strip()
        self.head_dir.joinpath("__init__.py").write_text(source)
        importlib.invalidate_caches()
        latest = importlib.import_module(self.head_package_path)
        if self.created_modules:
            raise NotImplementedError("You cannot write latest twice")
        self.created_modules.append(latest)
        return latest


class _FakeModuleWithEmptyClasses:
    EmptyEnum: type[Enum]
    EmptySchema: type[BaseModel]


@pytest.fixture()
def head_with_empty_classes(head_module_for: HeadModuleFor) -> _FakeModuleWithEmptyClasses:
    return head_module_for(
        """
        from enum import Enum, auto
        import pydantic

        class EmptyEnum(Enum):
            pass

        class EmptySchema(pydantic.BaseModel):
            pass
        """,
    )


class _FakeNamespaceWithOneStrField:
    SchemaWithOneStrField: type[BaseModel]


@pytest.fixture()
def head_with_one_str_field(head_module_for: HeadModuleFor) -> _FakeNamespaceWithOneStrField:
    return head_module_for(
        """
    from pydantic import BaseModel
    class SchemaWithOneStrField(BaseModel):
        foo: str
    """,
    )


@fixture_class(name="create_simple_versioned_packages")
class CreateSimpleVersionedPackages:
    api_version_var: ContextVar[date | None]
    temp_data_package_path: str
    create_versioned_packages: CreateVersionedPackages

    def __call__(self, *instructions: Any) -> tuple[ModuleType, ...]:
        return self.create_versioned_packages(version_change(*instructions))


@fixture_class(name="create_local_versioned_packages")
class CreateLocalVersionedPackages:
    api_version_var: ContextVar[date | None]
    temp_dir: Path
    created_modules: list[ModuleType]
    head_package_path: str

    def __call__(
        self,
        *version_changes: type[VersionChange],
        codegen_plugins: Sequence[CodegenPlugin] = DEFAULT_CODEGEN_PLUGINS,
        migration_plugins: Sequence[MigrationPlugin] = DEFAULT_CODEGEN_MIGRATION_PLUGINS,
        extra_context: dict[str, Any] | None = None,
    ) -> tuple[ModuleType, ...]:
        created_versions = versions(version_changes)

        generate_code_for_versioned_packages(
            importlib.import_module(self.head_package_path),
            VersionBundle(
                *created_versions,
                api_version_var=self.api_version_var,
            ),
            codegen_plugins=codegen_plugins,
            migration_plugins=migration_plugins,
            extra_context=extra_context,
        )
        importlib.invalidate_caches()

        return import_all_schemas(self.head_package_path, created_versions)


def import_all_schemas(head_package_path: str, created_versions: Sequence[Version]):
    return tuple(
        reversed(
            [
                importlib.import_module(
                    head_package_path.removesuffix("head") + f"{get_version_dir_name(version.value)}",
                )
                for version in created_versions
            ],
        ),
    )


@fixture_class(name="create_local_simple_versioned_packages")
class CreateLocalSimpleVersionedPackages:
    api_version_var: ContextVar[date | None]
    create_local_versioned_packages: CreateLocalVersionedPackages

    def __call__(
        self,
        *instructions: Any,
        codegen_plugins: Sequence[CodegenPlugin] = DEFAULT_CODEGEN_PLUGINS,
        migration_plugins: Sequence[MigrationPlugin] = DEFAULT_CODEGEN_MIGRATION_PLUGINS,
        extra_context: dict[str, Any] | None = None,
    ) -> ModuleType:
        return self.create_local_versioned_packages(
            version_change(*instructions),
            codegen_plugins=codegen_plugins,
            migration_plugins=migration_plugins,
            extra_context=extra_context,
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

    def __call__(
        self,
        *version_changes: type[VersionChange],
        head_version_changes: Sequence[type[VersionChange]] = (),
        router: VersionedAPIRouter | None = None,
    ) -> Cadwyn:
        router = router or self.router
        bundle = VersionBundle(
            HeadVersion(*head_version_changes),
            *versions(version_changes),
            api_version_var=self.api_version_var,
            head_schemas_package=importlib.import_module(self.temp_data_package_path + ".head"),
        )
        self.run_schema_codegen(bundle)
        app = Cadwyn(versions=bundle)
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
        "MyVersionChange",  # pyright: ignore[reportCallIssue]
        (VersionChange,),
        {
            "description": "",
            "instructions_to_migrate_to_previous_version": instructions,
            **body_items,
        },
    )


def serialize(enum: type[Enum]) -> dict[str, Any]:
    return {member.name: member.value for member in enum}
