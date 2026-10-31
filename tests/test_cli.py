import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cadwyn import __version__
from cadwyn.__main__ import app


def code(c: str) -> str:
    return "\n".join(line.rstrip() for line in textwrap.dedent(c.strip()).splitlines())


def test__render_module():
    result = CliRunner().invoke(
        app,
        [
            "render",
            "module",
            "tests._resources.render.classes",
            "--app=tests._resources.render.versions:app",
            "--version=2000-01-01",
            "--raw",
        ],
    )
    assert code(result.stdout) == code(
        """
from enum import Enum, auto
from pydantic import BaseModel

class MyEnum(Enum):
    pass

class A(BaseModel):
    pass
"""
    )


def test__render_model():
    result = CliRunner().invoke(
        app,
        [
            "render",
            "model",
            "tests._resources.render.classes:A",
            "--app=tests._resources.render.versions:app",
            "--version=2000-01-01",
            "--raw",
        ],
    )
    assert code(result.stdout) == code(
        """
class A(BaseModel):
    pass
"""
    )


@pytest.mark.parametrize("arg", ["-V", "--version"])
def test__cli_get_version(arg: str) -> None:
    result = CliRunner().invoke(app, [arg])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == f"Cadwyn {__version__}\n"


def _write_project_with_render_cli_args(directory: Path) -> list[str]:
    """Write a `main.py` app and a `data` package like a user's project, and return the CLI args to render it."""
    (directory / "data").mkdir()
    (directory / "data" / "__init__.py").touch()
    (directory / "data" / "schemas.py").write_text(
        code(
            """
from pydantic import BaseModel

class User(BaseModel):
    full_name: str
"""
        )
    )
    (directory / "main.py").write_text(
        code(
            """
from cadwyn import Cadwyn, Version, VersionBundle, VersionChange, schema
from data.schemas import User

class RenameNameToFullName(VersionChange):
    description = ""
    instructions_to_migrate_to_previous_version = (schema(User).field("full_name").had(name="name"),)

app = Cadwyn(versions=VersionBundle(Version("2001-01-01", RenameNameToFullName), Version("2000-01-01")))
"""
        )
    )
    return ["render", "module", "data.schemas", "--app=main:app", "--version=2000-01-01", "--raw"]


RENDERED_PROJECT_SCHEMAS = code(
    """
from pydantic import BaseModel

class User(BaseModel):
    name: str = Field()
"""
)


def test__cadwyn_console_script__run_from_directory_with_user_modules__should_import_them_from_that_directory(
    tmp_path: Path,
) -> None:
    args = _write_project_with_render_cli_args(tmp_path)
    console_script = shutil.which("cadwyn", path=Path(sys.executable).parent)
    assert console_script is not None

    result = subprocess.run(
        [console_script, *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert code(result.stdout) == RENDERED_PROJECT_SCHEMAS


def test__render_module__from_directory_not_on_sys_path__should_import_modules_from_that_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _write_project_with_render_cli_args(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    # `main` and `data` are imported into this process, so they must not leak into other tests
    try:
        result = CliRunner().invoke(app, args)
    finally:
        for module_name in ("main", "data", "data.schemas"):
            sys.modules.pop(module_name, None)

    assert result.exit_code == 0, result.output
    assert code(result.stdout) == RENDERED_PROJECT_SCHEMAS
