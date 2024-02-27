import importlib
import inspect
import textwrap

import pytest
from typer.testing import CliRunner

from cadwyn import __version__
from cadwyn.__main__ import app as cadwyn_typer_app
from tests.conftest import _FakeModuleWithEmptyClasses


@pytest.fixture(autouse=True)
def _resources(temp_data_dir, latest_with_empty_classes: _FakeModuleWithEmptyClasses):
    temp_data_dir.joinpath("__init__.py")
    temp_data_dir.joinpath("my_cli.py").write_text(
        textwrap.dedent(
            """
    from contextvars import ContextVar
    from datetime import date

    from cadwyn.structure import Version, VersionBundle, VersionChange, schema

    from . import latest

    api_version_var = ContextVar("api_version")


    class VersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            schema(latest.EmptySchema).field("foo").existed_as(type=str),
        ]


    version_bundle = VersionBundle(
        Version(date(2001, 1, 1), VersionChange1),
        Version(date(2000, 1, 1)),
        latest_schemas_package=latest,
    )


    def callable_that_returns_version_bundle():
        return version_bundle


    def invalid_callable_that_has_arguments(_arg):
        raise NotImplementedError


    def invalid_callable_that_returns_non_version_bundle():
        return 83

    """,
        ),
    )
    importlib.invalidate_caches()


def _assert_codegen_migrations_were_applied(data_package_path):
    importlib.invalidate_caches()
    v2000_01_01 = importlib.import_module(data_package_path + ".v2000_01_01")
    v2001_01_01 = importlib.import_module(data_package_path + ".v2001_01_01")

    assert inspect.getsource(v2000_01_01.EmptySchema) == "class EmptySchema(pydantic.BaseModel):\n    foo: str\n"
    assert inspect.getsource(v2001_01_01.EmptySchema) == "class EmptySchema(pydantic.BaseModel):\n    pass\n"


@pytest.mark.parametrize("arg", ["-V", "--version"])
def test__cli_get_version(arg: str) -> None:
    result = CliRunner().invoke(cadwyn_typer_app, [arg])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == f"Cadwyn {__version__}\n"


@pytest.mark.parametrize("variable_name_to_use", ["version_bundle", "callable_that_returns_version_bundle"])
def test__cli_codegen(
    temp_data_package_path: str,
    variable_name_to_use: str,
    latest_with_empty_classes,
    latest_package_path,
    data_package_path,
) -> None:
    result = CliRunner().invoke(
        cadwyn_typer_app,
        [
            "codegen",
            temp_data_package_path + f".my_cli:{variable_name_to_use}",
        ],
    )
    assert result.exit_code == 0, result.stdout

    _assert_codegen_migrations_were_applied(temp_data_package_path)


@pytest.mark.parametrize("variable_name_to_use", ["version_bundle", "callable_that_returns_version_bundle"])
def test__deprecated_cli_codegen_should_raise_deprecation_warning(
    temp_data_package_path: str,
    variable_name_to_use: str,
    latest_with_empty_classes,
    latest_package_path,
    data_package_path,
) -> None:
    with pytest.warns(DeprecationWarning, match="`cadwyn generate-code-for-versioned-packages` is deprecated"):
        result = CliRunner().invoke(
            cadwyn_typer_app,
            [
                "generate-code-for-versioned-packages",
                latest_package_path,
                temp_data_package_path + f".my_cli:{variable_name_to_use}",
            ],
        )
    assert result.exit_code == 0, result.stdout

    _assert_codegen_migrations_were_applied(temp_data_package_path)


@pytest.mark.parametrize(
    "variable_name_to_use",
    ["invalid_callable_that_has_arguments", "invalid_callable_that_returns_non_version_bundle", "VersionChange1"],
)
def test__deprecated_cli_codegen__with_invalid_version_bundle_arg__should_raise_type_error(
    temp_data_package_path,
    latest_package_path: str,
    variable_name_to_use: str,
) -> None:
    result = CliRunner(mix_stderr=False).invoke(
        cadwyn_typer_app,
        [
            "generate-code-for-versioned-packages",
            latest_package_path,
            temp_data_package_path + f".my_cli:{variable_name_to_use}",
        ],
    )
    assert result.exit_code == 1
    assert type(result.exception) == TypeError
    assert str(result.exception).startswith(
        "The provided version bundle is not a version bundle and is not a zero-argument "
        "callable that returns the version bundle. Instead received:",
    )
