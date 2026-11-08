import functools
import inspect
from collections.abc import Iterator
from typing import Any, Union, get_type_hints

import pytest

import cadwyn
import cadwyn.structure
from cadwyn.schema_generation import SchemaGenerator


def _public_annotation_targets() -> Iterator[Any]:
    for module in (cadwyn, cadwyn.structure):
        for name in module.__all__:
            obj = getattr(module, name)
            yield pytest.param(obj, id=f"{module.__name__}.{name}")
            if inspect.isclass(obj):
                yield from _public_method_annotation_targets(obj, f"{module.__name__}.{name}")
    # SchemaGenerator is also part of the supported API, through cadwyn.schema_generation.
    yield pytest.param(SchemaGenerator, id="SchemaGenerator")
    yield from _public_method_annotation_targets(SchemaGenerator, "SchemaGenerator")


def _public_method_annotation_targets(cls: type, prefix: str) -> Iterator[Any]:
    for name, method in inspect.getmembers_static(cls):
        if name.startswith("_") and not name.startswith("__"):
            continue
        if isinstance(method, classmethod | staticmethod):
            method = method.__func__
        if isinstance(method, property):
            method = method.fget
        if isinstance(method, functools.cached_property):
            method = method.func
        # Inherited FastAPI/Starlette methods retain their upstream annotations.
        if inspect.isfunction(method) and method.__module__.startswith("cadwyn."):
            yield pytest.param(method, id=f"{prefix}.{name}")


@pytest.mark.parametrize("obj", list(_public_annotation_targets()))
def test__public_api_annotations__when_resolved_at_runtime__should_succeed(obj: Any):
    get_type_hints(obj)


@pytest.mark.parametrize("base", [cadwyn.VersionChange, cadwyn.VersionChangeWithSideEffects])
def test__version_change_annotations__on_user_subclass__should_resolve_version_bundle(base: type[cadwyn.VersionChange]):
    class UserChange(base):
        description = "An introspectable user-defined version change."
        instructions_to_migrate_to_previous_version = ()

    assert get_type_hints(UserChange)["_bound_version_bundle"] == Union[cadwyn.VersionBundle, None]
