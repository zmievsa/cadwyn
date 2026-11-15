"""The modules that moved from `cadwyn` to `cadwyn-core` keep working under their old names."""

import importlib
import inspect
import pickle
import subprocess
import sys
from types import ModuleType

import pytest

import cadwyn.exceptions
import cadwyn.structure
import cadwyn_core.exceptions
import cadwyn_core.structure
import cadwyn_core.structure.versions

# The moved packages stay packages in `cadwyn`, because `cadwyn.structure.versions` still has the submodules that
# need FastAPI. The other moved modules are the same module objects under their old names.
_MOVED_PACKAGES = ["structure", "structure.versions"]
_MOVED_MODULES = [
    "_asts",
    "_utils",
    *(f"structure.{name}" for name in ("common", "data", "endpoints", "enums", "schemas")),
    *(
        f"structure.versions.{name}"
        for name in (
            "_aliases",
            "_bundle",
            "_changes",
            "_common",
            "_core_schema",
            "_enums",
            "_json",
            "_migration_targets",
            "_migrations",
        )
    ),
]


@pytest.mark.parametrize("name", _MOVED_MODULES)
def test__old_name_of_moved_module__should_be_the_same_module(name: str):
    module = importlib.import_module(f"cadwyn_core.{name}")
    parent_name, _, attribute = f"cadwyn.{name}".rpartition(".")

    assert importlib.import_module(f"cadwyn.{name}") is module
    assert getattr(importlib.import_module(parent_name), attribute) is module


@pytest.mark.parametrize("name", _MOVED_PACKAGES)
def test__old_name_of_moved_package__should_have_every_name_of_the_moved_package(name: str):
    package = importlib.import_module(f"cadwyn_core.{name}")
    old_package = importlib.import_module(f"cadwyn.{name}")

    for attribute, value in vars(package).items():
        if attribute.startswith("__"):
            continue
        if isinstance(value, ModuleType) and value.__name__ == f"{package.__name__}.{attribute}":
            # Its own submodule, which has its old name too
            expected = importlib.import_module(f"{old_package.__name__}.{attribute}")
        else:
            expected = value
        assert getattr(old_package, attribute) is expected, attribute


def test__old_name_of_moved_package__should_keep_its_public_names():
    assert cadwyn.structure.__all__ == cadwyn_core.structure.__all__


@pytest.mark.parametrize("name", _MOVED_MODULES)
def test__old_name_of_moved_module__in_a_new_process__should_be_the_same_module(name: str):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import cadwyn.{name}, cadwyn_core.{name}, sys; "
                f"assert sys.modules['cadwyn.{name}'] is cadwyn.{name} is cadwyn_core.{name}"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test__patch__through_old_name_of_moved_module__should_patch_the_moved_module(monkeypatch: pytest.MonkeyPatch):
    sentinel = object()
    monkeypatch.setattr("cadwyn._utils.Sentinel", sentinel)

    assert importlib.import_module("cadwyn_core._utils").Sentinel is sentinel


_ERRORS_OF_CADWYN_CORE = [
    error
    for _, error in inspect.getmembers(cadwyn_core.exceptions, inspect.isclass)
    if issubclass(error, Exception) and error.__module__ == cadwyn_core.exceptions.__name__
]


def test__errors_of_cadwyn_core__should_be_the_core_ones():
    assert {error.__name__ for error in _ERRORS_OF_CADWYN_CORE} == {
        "CadwynError",
        "CadwynHeadRequestValidationError",
        "CadwynRenderError",
        "CadwynStructureError",
        "InvalidGenerationInstructionError",
        "LintingError",
        "SchemaGenerationError",
    }


@pytest.mark.parametrize("error", _ERRORS_OF_CADWYN_CORE, ids=lambda error: error.__name__)
def test__error_of_cadwyn_core__should_be_importable_from_cadwyn_exceptions(error: type[Exception]):
    assert getattr(cadwyn.exceptions, error.__name__) is error


@pytest.mark.parametrize(
    "error",
    # Cadwyn 9.0 couldn't unpickle its pickles of CadwynHeadRequestValidationError, as they called it with the message
    [error for error in _ERRORS_OF_CADWYN_CORE if error is not cadwyn_core.exceptions.CadwynHeadRequestValidationError],
    ids=lambda error: error.__name__,
)
def test__pickle_of_cadwyn_9_0__with_error_of_cadwyn_core__should_unpickle(error: type[Exception]):
    # Protocol 0 names the class by its module and name, as pickles of every protocol do, in a form that can be edited
    pickled = pickle.dumps(Exception("message"), protocol=0).replace(
        b"cexceptions\nException\n", f"ccadwyn.exceptions\n{error.__name__}\n".encode()
    )

    unpickled = pickle.loads(pickled)  # It's our own pickle

    assert (type(unpickled), unpickled.args) == (error, ("message",))


_DEFINITIONS_IN_MOVED_STRUCTURE = [
    pytest.param(value, id=f"{value.__module__}.{name}")
    for module in (
        # Its submodules give their definitions to the package
        cadwyn_core.structure.versions,
        *(importlib.import_module(f"cadwyn_core.{name}") for name in _MOVED_MODULES if name.count(".") == 1),
    )
    for name, value in vars(module).items()
    if (inspect.isclass(value) or inspect.isfunction(value))
    and value.__module__ == module.__name__
    and value.__qualname__ == name
]


@pytest.mark.parametrize("definition", _DEFINITIONS_IN_MOVED_STRUCTURE)
def test__pickle_of_cadwyn_9_0__with_moved_definition__should_unpickle(definition: object):
    old_module = definition.__module__.replace("cadwyn_core.", "cadwyn.", 1)
    # Protocol 0 names the definition by its module and name, in a form that can be edited
    pickled = pickle.dumps(definition, protocol=0).replace(
        f"c{definition.__module__}\n".encode(), f"c{old_module}\n".encode()
    )

    assert f"c{old_module}\n".encode() in pickled
    assert pickle.loads(pickled) is definition  # It's our own pickle
