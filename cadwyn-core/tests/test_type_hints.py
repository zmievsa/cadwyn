"""Runtime annotation introspection, e.g. `typing.get_type_hints()`, works on all of cadwyn-core.

Pydantic, documentation generators, and runtime type checkers resolve annotations. This test checks whatever is
installed: the `standalone` tox environment runs it without Starlette and FastAPI, and Cadwyn's own tests run it with
them, with and without the FastAPI integration imported.
"""

import importlib
import inspect
import pkgutil
import typing
from collections.abc import Iterator

from pydantic import BaseModel

import cadwyn_core
from cadwyn_core import (
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    migrate_request,
    migrate_response,
)


def _iter_modules() -> Iterator[object]:
    yield cadwyn_core
    for module_info in pkgutil.walk_packages(cadwyn_core.__path__, f"{cadwyn_core.__name__}."):
        yield importlib.import_module(module_info.name)


def _iter_annotated_objects() -> Iterator[tuple[str, object]]:
    """Yield the classes, functions, and methods of cadwyn-core, with their names"""
    seen: set[int] = set()
    to_visit = [
        (f"{vars(module)['__name__']}.{name}", value)
        for module in _iter_modules()
        for name, value in vars(module).items()
    ]
    while to_visit:
        name, value = to_visit.pop()
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, property):
            to_visit.extend(
                (f"{name}.{accessor.__name__}", accessor)
                for accessor in (value.fget, value.fset, value.fdel)
                if accessor is not None
            )
            continue
        if id(value) in seen or not (inspect.isclass(value) or inspect.isfunction(value)):
            continue
        module_name = getattr(value, "__module__", None) or ""
        if module_name.partition(".")[0] != cadwyn_core.__name__:
            continue
        seen.add(id(value))
        yield name, value
        if inspect.isclass(value):
            to_visit.extend((f"{name}.{attribute}", member) for attribute, member in vars(value).items())


def test__get_type_hints__on_every_object_of_cadwyn_core__should_resolve_its_annotations():
    errors = {}
    for name, value in _iter_annotated_objects():
        try:
            typing.get_type_hints(value, include_extras=True)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            errors[name] = f"{type(exc).__name__}: {exc}"

    assert errors == {}


class _User(BaseModel):
    name: str


def test__type_hints_of_request_and_response_info__should_be_the_classes_that_migrations_get():
    got = {}

    class GetWhatMigrationsGet(VersionChange):
        description = "Gets what the migrations get"
        instructions_to_migrate_to_previous_version = ()

        @convert_request_to_next_version_for(_User)
        def get_request(request: RequestInfo):
            got["request headers"] = type(request.headers)

        @convert_response_to_previous_version_for(_User)
        def get_response(response: ResponseInfo):
            got["response"] = type(response._response)
            got["response headers"] = type(response.headers)

    versions = VersionBundle(Version("2001-01-01", GetWhatMigrationsGet), Version("2000-01-01"))
    migrate_request(versions, _User, body={"name": "John"}, version="2000-01-01")
    migrate_response(versions, _User, body=_User(name="John"), version="2000-01-01")

    assert got == {
        "request headers": typing.get_type_hints(RequestInfo.headers.fget)["return"],
        "response": typing.get_type_hints(ResponseInfo.__init__)["response"],
        "response headers": typing.get_type_hints(ResponseInfo.headers.fget)["return"],
    }
