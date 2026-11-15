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
import cadwyn_core.schema_generation
import cadwyn_core.structure
import cadwyn_core.structure.versions
from cadwyn._asts import get_fancy_repr
from cadwyn._utils import lenient_issubclass
from cadwyn.schema_generation._schema_generator import SchemaGenerator
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import RequestInfo
from cadwyn.structure.endpoints import endpoint
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions._bundle import VersionBundle
from cadwyn.structure.versions._changes import VersionChange

# The moved packages stay packages in `cadwyn`, because `cadwyn.structure.versions` still has the submodules that
# need FastAPI. The other moved modules are the same module objects under their old names.
_MOVED_PACKAGES = ["schema_generation", "structure", "structure.versions"]
_MOVED_MODULES = [
    "_asts",
    "_utils",
    *(
        f"schema_generation.{name}"
        for name in (
            "_annotation_evaluation",
            "_annotation_transformer",
            "_callables",
            "_common",
            "_core_schema",
            "_enums",
            "_fields",
            "_generics",
            "_instructions",
            "_models",
            "_references",
            "_schema_generator",
            "_versioned_models",
        )
    ),
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


@pytest.mark.parametrize("first_package", ["cadwyn", "cadwyn_core"])
@pytest.mark.parametrize("name", _MOVED_MODULES)
def test__old_name_of_moved_module__in_a_new_process__should_be_the_same_module(name: str, first_package: str):
    second_package = "cadwyn_core" if first_package == "cadwyn" else "cadwyn"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import {first_package}.{name}, {second_package}.{name}, sys; "
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
        # Their submodules give their definitions to the packages
        cadwyn_core.schema_generation,
        cadwyn_core.structure.versions,
        *(
            importlib.import_module(f"cadwyn_core.structure.{name}")
            for name in ("common", "data", "endpoints", "enums", "schemas")
        ),
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


def test__names_imported_from_old_names_of_moved_modules__should_be_the_moved_ones():
    # The imports above are type checked too, which needs the names to be visible under the old names statically
    import cadwyn_core._asts
    import cadwyn_core._utils
    import cadwyn_core.schema_generation._schema_generator
    import cadwyn_core.structure.common
    import cadwyn_core.structure.data
    import cadwyn_core.structure.endpoints
    import cadwyn_core.structure.enums
    import cadwyn_core.structure.schemas
    import cadwyn_core.structure.versions._bundle
    import cadwyn_core.structure.versions._changes

    assert get_fancy_repr is cadwyn_core._asts.get_fancy_repr
    assert lenient_issubclass is cadwyn_core._utils.lenient_issubclass
    assert SchemaGenerator is cadwyn_core.schema_generation._schema_generator.SchemaGenerator
    assert VersionType is cadwyn_core.structure.common.VersionType
    assert RequestInfo is cadwyn_core.structure.data.RequestInfo
    assert endpoint is cadwyn_core.structure.endpoints.endpoint
    assert enum is cadwyn_core.structure.enums.enum
    assert schema is cadwyn_core.structure.schemas.schema
    assert VersionBundle is cadwyn_core.structure.versions._bundle.VersionBundle
    assert VersionChange is cadwyn_core.structure.versions._changes.VersionChange
