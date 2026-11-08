from collections.abc import (
    Hashable,
    Iterator,
)
from typing import (
    Any,
    Union,
    cast,
)

from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute


def _iter_dependants_that_fastapi_solves(  # pragma: no cover # FastAPI >= 0.158.2
    dependant: Dependant, route: APIRoute
) -> Iterator[Dependant]:
    """Yield the dependant and every sub-dependant in the tree that FastAPI solves for the route.

    Like FastAPI, we solve the dependant of an override instead of the overridden dependency.
    """
    dependency_overrides = _get_dependency_overrides(route)
    dependants = [dependant]
    while dependants:
        dependant = dependants.pop()
        yield dependant
        for sub_dependant in dependant.dependencies:
            if dependency_overrides and (call := dependency_overrides.get(sub_dependant.call)) is not None:
                sub_dependant = get_dependant(
                    path=cast("str", sub_dependant.path), call=call, name=sub_dependant.name, scope=sub_dependant.scope
                )
            dependants.append(sub_dependant)


def _get_dependency_overrides(route: APIRoute) -> dict[Any, Any]:
    return getattr(route.dependency_overrides_provider, "dependency_overrides", None) or {}


def _iter_dependencies(
    dependant: Dependant, dependency_overrides: dict[Any, Any]
) -> Iterator[tuple[Hashable, Union[Dependant, None]]]:
    """Yield a key of every dependency in the tree that FastAPI solves, with the dependency's dependant.

    The keys of the same dependency are equal in the trees of all versions. Like FastAPI's dependency cache keys,
    they consist of the callable that FastAPI calls, all security scopes of the dependency, and its scope. Versioned
    routes use copies of dependencies that compare equal to the originals, and FastAPI already hashes these
    callables. Overrides come from the same dict for every route but can be unhashable, so we key them by identity,
    together with the overridden callable (a key of that dict) because FastAPI keeps dependencies with the same
    override apart.
    FastAPI solves the dependant of an override instead of the overridden dependency, so for an overridden
    dependency we yield no dependant and skip its sub-dependencies.
    """
    for sub_dependant in dependant.dependencies:
        call = sub_dependant.call
        scopes = tuple(sorted({*(sub_dependant.parent_oauth_scopes or ()), *(sub_dependant.own_oauth_scopes or ())}))
        # Same lookup as in FastAPI, which only looks up overrides when there are any
        if dependency_overrides and (override := dependency_overrides.get(call, call)) is not call:
            yield (_OVERRIDE_KEY, call, id(override), scopes, sub_dependant.scope), None
        else:
            yield (call, scopes, sub_dependant.scope), sub_dependant
            yield from _iter_dependencies(sub_dependant, dependency_overrides)


# Overrides stay alive in the overrides dict, so their ids identify them while we compare the trees
_OVERRIDE_KEY = object()


def _has_other_body_readers(head_dependant: Dependant, head_route: APIRoute) -> bool:
    """Whether a dependency of the head reads the body with another type than the body param (e.g. a dict).

    It would read the JSON that we encode instead of what the client has sent. A dependency that reads the body into
    the same type validates it as the body param does. FastAPI builds the dependants of the overrides from what they
    are on each request (e.g. their signatures), so we check them on each request that we encode as well.
    """
    field_info = head_dependant.body_params[0].field_info
    return any(
        field.field_info.annotation is not field_info.annotation
        or len(field.field_info.metadata) != len(field_info.metadata)
        or any(
            item is not body_item
            for item, body_item in zip(field.field_info.metadata, field_info.metadata, strict=True)
        )
        for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
        if dependant is not head_dependant
        for field in dependant.body_params
    )
