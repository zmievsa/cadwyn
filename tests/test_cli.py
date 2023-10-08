import importlib
import inspect

import pytest
from typer.testing import CliRunner

from cadwyn import __version__
from cadwyn.__main__ import app as cadwyn_typer_app


def _assert_codegen_migrations_were_applied(data_package_path):
    v2000_01_01 = importlib.import_module(data_package_path + ".v2000_01_01")
    v2001_01_01 = importlib.import_module(data_package_path + ".v2001_01_01")

    assert inspect.getsource(v2000_01_01.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    pass\n"

    assert (
        inspect.getsource(v2001_01_01.SchemaWithOneStrField)
        == "class SchemaWithOneStrField(BaseModel):\n    foo: str = Field(default='foo')\n"
    )


@pytest.mark.parametrize("arg", ["-V", "--version"])
def test__cli_get_version(arg: str) -> None:
    result = CliRunner().invoke(cadwyn_typer_app, [arg])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == f"Cadwyn {__version__}\n"


@pytest.mark.parametrize("variable_name_to_use", ["version_bundle", "callable_that_returns_version_bundle"])
def test__cli_codegen(data_package_path: str, latest_module_path: str, variable_name_to_use: str) -> None:
    result = CliRunner().invoke(
        cadwyn_typer_app,
        [
            "generate-code-for-versioned-packages",
            latest_module_path,
            data_package_path + f".cli_utils:{variable_name_to_use}",
        ],
    )
    assert result.exit_code == 0, result.stdout

    _assert_codegen_migrations_were_applied(data_package_path)


@pytest.mark.parametrize(
    "variable_name_to_use",
    ["invalid_callable_that_has_arguments", "invalid_callable_that_returns_non_version_bundle", "VersionChange1"],
)
def test__cli_codegen__with_invalid_version_bundle_arg__should_raise_type_error(
    data_package_path: str,
    latest_module_path: str,
    variable_name_to_use: str,
) -> None:
    result = CliRunner(mix_stderr=False).invoke(
        cadwyn_typer_app,
        [
            "generate-code-for-versioned-packages",
            latest_module_path,
            data_package_path + f".cli_utils:{variable_name_to_use}",
        ],
    )
    assert result.exit_code == 1
    assert type(result.exception) == TypeError
    assert str(result.exception).startswith(
        "The provided version bundle is not a version bundle and is not a zero-argument "
        "callable that returns the version bundle. Instead received:",
    )
