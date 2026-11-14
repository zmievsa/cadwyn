"""Versioning must work without FastAPI and its dependencies, with nothing but Pydantic and what Pydantic requires.

Standalone use isn't available yet (https://github.com/zmievsa/cadwyn/issues/203): the `cadwyn` package still imports
the FastAPI integration, so these tests skip its `__init__` module and check the modules that standalone use will be
built from. They run in a process that can't import any installed package besides Cadwyn, Pydantic, and what Pydantic
requires.
"""

import ast
import importlib.metadata
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from packaging.requirements import Requirement

import cadwyn


def _get_import_names_of_pydantic_and_its_requirements() -> set[str]:
    distributions = {"pydantic"}
    to_visit = ["pydantic"]
    while to_visit:
        for requirement in map(Requirement, importlib.metadata.requires(to_visit.pop()) or ()):
            if (requirement.marker is None or requirement.marker.evaluate()) and requirement.name not in distributions:
                distributions.add(requirement.name)
                to_visit.append(requirement.name)
    normalized = {name.replace("-", "_").lower() for name in distributions}
    return {
        import_name
        for import_name, import_distributions in importlib.metadata.packages_distributions().items()
        if any(distribution.replace("-", "_").lower() in normalized for distribution in import_distributions)
    }


_BLOCK_OTHER_INSTALLED_PACKAGES = textwrap.dedent(
    """
    import builtins
    import importlib.abc
    import importlib.machinery
    import sys
    import sysconfig
    import types

    cadwyn_dir, allowed_packages, modules_of_fastapi_integration = sys.argv[1:]
    allowed = {"cadwyn", *allowed_packages.split(",")}
    fastapi_integration = set(modules_of_fastapi_integration.split(","))
    installed_package_dirs = tuple({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})


    class OnlyAllowedInstalledPackages(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname in fastapi_integration:
                raise ModuleNotFoundError(f"{fullname} is a module of the FastAPI integration", name=fullname)
            if fullname.partition(".")[0] in allowed:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and (spec.origin or "").startswith(installed_package_dirs):
                raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
            return None


    sys.meta_path.insert(0, OnlyAllowedInstalledPackages())
    # Modules that are imported already (e.g. coverage, which measures this process) skip the finders
    original_import = builtins.__import__


    def import_only_allowed_installed_packages(name, globals=None, locals=None, fromlist=(), level=0):
        module = original_import(name, globals, locals, fromlist, level)
        top_level_name = name.partition(".")[0]
        importer = (globals or {}).get("__name__") or ""
        if level == 0 and importer.partition(".")[0] == "cadwyn" and top_level_name not in allowed:
            if (getattr(sys.modules.get(top_level_name), "__file__", None) or "").startswith(installed_package_dirs):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return module


    builtins.__import__ = import_only_allowed_installed_packages
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("FastAPI must not be importable")

    # The `__init__` module of `cadwyn` still imports the FastAPI integration
    cadwyn = types.ModuleType("cadwyn")
    cadwyn.__path__ = [cadwyn_dir]
    sys.modules["cadwyn"] = cadwyn
    """
)


# The modules of Cadwyn that need FastAPI, which the modules that standalone use is built from must not import
_MODULES_OF_FASTAPI_INTEGRATION = frozenset(
    (
        "cadwyn.__main__",
        "cadwyn._importer",
        "cadwyn._internal.fastapi_bindings",
        "cadwyn._render",
        "cadwyn.applications",
        "cadwyn.dependencies",
        "cadwyn.middleware",
        "cadwyn.route_generation",
        "cadwyn.routing",
        *(
            f"cadwyn.structure.versions.{name}"
            for name in (
                "_background",
                "_body_inputs",
                "_compat",
                "_dependencies",
                "_forms",
                "_request_body",
                "_requests",
                "_responses",
                "_uploads",
                "_versioned_endpoints",
            )
        ),
    )
)
_CADWYN_DIR = Path(cadwyn.__file__).parent
_MODULES_OF_STANDALONE_USE = sorted(
    name
    for path in (
        _CADWYN_DIR / "_utils.py",
        _CADWYN_DIR / "_asts.py",
        _CADWYN_DIR / "exceptions.py",
        *(_CADWYN_DIR / "structure").rglob("*.py"),
        *(_CADWYN_DIR / "schema_generation").rglob("*.py"),
    )
    if (name := ".".join(path.relative_to(_CADWYN_DIR.parent).with_suffix("").parts).removesuffix(".__init__"))
    not in _MODULES_OF_FASTAPI_INTEGRATION
)


@pytest.mark.parametrize("module_name", _MODULES_OF_STANDALONE_USE)
def test__module_of_standalone_use__should_not_import_the_fastapi_integration_even_for_type_checking(module_name: str):
    path = _CADWYN_DIR.parent.joinpath(*module_name.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]

    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module
            )
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)

    assert not imported & _MODULES_OF_FASTAPI_INTEGRATION


def _run_without_fastapi(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _BLOCK_OTHER_INSTALLED_PACKAGES + textwrap.dedent(code),
            str(Path(cadwyn.__file__).parent),
            ",".join(sorted(_get_import_names_of_pydantic_and_its_requirements())),
            ",".join(sorted(_MODULES_OF_FASTAPI_INTEGRATION)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "module_name", ["cadwyn._utils", "cadwyn.exceptions", "cadwyn.schema_generation", "cadwyn.structure"]
)
def test__import__without_fastapi_and_its_dependencies__should_succeed(module_name: str):
    result = _run_without_fastapi(f"import {module_name}")

    assert result.returncode == 0, result.stderr


_VERSIONS_OF_USERS = """
    from pydantic import BaseModel, Field

    from cadwyn.exceptions import CadwynError
    from cadwyn.schema_generation import generate_versioned_models, migrate_request, migrate_response
    from cadwyn.structure import (
        MigratedRequest,
        MigratedResponse,
        RequestInfo,
        ResponseInfo,
        Version,
        VersionBundle,
        VersionChange,
        convert_request_to_next_version_for,
        convert_response_to_previous_version_for,
        schema,
    )


    class User(BaseModel):
        full_name: str


    headers_of_migrations = []


    class RenameNameToFullName(VersionChange):
        description = "'name' is now called 'full_name'"
        instructions_to_migrate_to_previous_version = (schema(User).field("full_name").had(name="name"),)

        @convert_request_to_next_version_for(User)
        def rename_name_to_full_name(request: RequestInfo):
            headers_of_migrations.append(request.headers)
            request.body["full_name"] = request.body.pop("name")

        @convert_response_to_previous_version_for(User)
        def rename_full_name_to_name(response: ResponseInfo):
            headers_of_migrations.append(response.headers)
            response.body["name"] = response.body.pop("full_name")


    versions = VersionBundle(Version("2001-01-01", RenameNameToFullName), Version("2000-01-01"))
"""


def test__generate_versioned_models__without_fastapi_and_its_dependencies__should_generate_the_models():
    result = _run_without_fastapi(
        _VERSIONS_OF_USERS
        + """
    assert list(generate_versioned_models(versions)["2000-01-01"][User].model_fields) == ["name"]


    class Marker:
        pass


    class Item(BaseModel):
        value: int = Field(json_schema_extra={"marker": Marker()})


    class ChangeTheMarker(VersionChange):
        description = "Compares attributes of types that Cadwyn doesn't know"
        instructions_to_migrate_to_previous_version = (
            schema(Item).field("value").had(json_schema_extra={"marker": Marker()}),
        )


    other_versions = VersionBundle(Version("2001-01-01", ChangeTheMarker), Version("2000-01-01"))
    assert generate_versioned_models(other_versions)["2000-01-01"][Item].model_fields["value"].json_schema_extra
    """
    )

    assert result.returncode == 0, result.stderr


def test__migrate_request__without_fastapi_and_its_dependencies__should_migrate_the_request_without_headers():
    result = _run_without_fastapi(
        _VERSIONS_OF_USERS
        + """
    migrated = migrate_request(
        versions, User, body={"name": "John"}, version="2000-01-01", cookies={"a": "b"}, query_params={"c": "d"}
    )
    assert migrated == MigratedRequest(
        body=User(full_name="John"), headers=None, cookies={"a": "b"}, query_params={"c": "d"}
    )
    assert headers_of_migrations == [None]
    """
    )

    assert result.returncode == 0, result.stderr


def test__migrate_response__without_fastapi_and_its_dependencies__should_migrate_the_response_without_headers():
    result = _run_without_fastapi(
        _VERSIONS_OF_USERS
        + """
    migrated = migrate_response(versions, User, body=User(full_name="John"), version="2000-01-01", status_code=201)
    assert migrated.body.model_dump() == {"name": "John"}
    assert (migrated.status_code, migrated.headers) == (201, None)
    assert headers_of_migrations == [None]
    """
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "migrate",
    [
        'migrate_request(versions, User, body={"name": "John"}, version="2000-01-01", headers={"a": "b"})',
        'migrate_response(versions, User, body={"full_name": "John"}, version="2000-01-01", headers={"a": "b"})',
    ],
)
def test__migrate_request_and_response__with_headers_without_starlette__should_raise_cadwyn_error(migrate: str):
    result = _run_without_fastapi(
        _VERSIONS_OF_USERS
        + f"""
    try:
        {migrate}
    except CadwynError as error:
        assert str(error) == "Migrating headers needs Starlette. Install it, e.g. with `pip install starlette`."
    else:
        raise AssertionError("Passing headers without Starlette must fail")
    """
    )

    assert result.returncode == 0, result.stderr
