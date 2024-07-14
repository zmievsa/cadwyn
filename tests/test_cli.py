import sys
import textwrap

import pytest
from typer.testing import CliRunner

from cadwyn import __version__
from cadwyn.__main__ import app


def code(c: str) -> str:
    return textwrap.dedent(c.strip())


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


def test__render_model__with_syntax_highlighting():  # pragma: no cover
    result = CliRunner().invoke(
        app,
        [
            "render",
            "model",
            "tests._resources.render.classes:A",
            "--app=tests._resources.render.versions:app",
            "--version=2000-01-01",
        ],
    )
    assert result.exit_code == 0

    if sys.platform.startswith("win32"):
        # Windows rendering is weird
        return

    assert code(result.stdout) == (
        "1 class A(BaseModel):                                                         \n  2     pass"
    )


@pytest.mark.parametrize("arg", ["-V", "--version"])
def test__cli_get_version(arg: str) -> None:
    result = CliRunner().invoke(app, [arg])
    assert result.exit_code == 0, result.stdout
    assert result.stdout == f"Cadwyn {__version__}\n"
