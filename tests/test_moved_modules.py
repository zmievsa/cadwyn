"""The modules that moved from `cadwyn` to `cadwyn-core` keep working under their old names."""

import importlib
import inspect
import pickle
import subprocess
import sys

import pytest

import cadwyn
import cadwyn.exceptions
import cadwyn_core.exceptions


@pytest.mark.parametrize("name", ["_asts", "_utils"])
def test__old_name_of_moved_module__should_be_the_same_module(name: str):
    module = importlib.import_module(f"cadwyn_core.{name}")

    assert importlib.import_module(f"cadwyn.{name}") is module
    assert getattr(cadwyn, name) is module


@pytest.mark.parametrize("name", ["_asts", "_utils"])
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
