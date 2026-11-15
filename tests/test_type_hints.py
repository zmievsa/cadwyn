"""Runtime annotation introspection, e.g. `typing.get_type_hints()`, works on all of cadwyn-core with FastAPI installed.

cadwyn-core's own test of it runs here with the FastAPI integration imported and without it. The `standalone` tox
environment runs it without Starlette and FastAPI.
"""

import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import cadwyn
import cadwyn_core
from cadwyn_core import _stand_ins

_TEST_OF_CADWYN_CORE = Path(__file__).parents[1] / "cadwyn-core" / "tests" / "test_type_hints.py"


@pytest.mark.parametrize("first_import", ["cadwyn", "cadwyn_core"])
def test__type_hints_of_cadwyn_core__should_resolve(first_import: str):
    result = subprocess.run(
        # Pytest imports the package first, as a plugin
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", first_import, str(_TEST_OF_CADWYN_CORE)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test__modules_of_cadwyn__with_fastapi_integration__should_have_no_stand_ins():
    assert cadwyn.fastapi_bindings is not None
    for module_info in pkgutil.walk_packages(cadwyn_core.__path__, f"{cadwyn_core.__name__}."):
        importlib.import_module(module_info.name)

    stand_ins = [
        f"{name}.{attribute}"
        for name, module in list(sys.modules.items())
        if name.partition(".")[0] in {"cadwyn", "cadwyn_core"}
        for attribute, value in vars(module).items()
        if _stand_ins.get_class_stood_for(value) is not None
    ]

    assert stand_ins == []


@pytest.mark.parametrize("fastapi_first", [True, False])
def test__type_hints_of_cadwyn_core__with_fastapi_but_without_cadwyn__should_accept_objects_of_fastapi(
    fastapi_first: bool,
):
    imports = ["fastapi.params", "cadwyn_core"] if fastapi_first else ["cadwyn_core", "fastapi.params"]
    code = f"""
import {imports[0]}, {imports[1]}, sys, typing
from cadwyn_core import endpoint

(depends,) = typing.get_args(typing.get_type_hints(endpoint("/", ["GET"]).had)["dependencies"])
assert "cadwyn" not in sys.modules
assert isinstance(fastapi.params.Depends(), depends)
assert issubclass(fastapi.params.Depends, depends)
# FastAPI's own class if it's imported first, otherwise a stand-in that checks objects against it
assert (depends is fastapi.params.Depends) is {fastapi_first}
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
