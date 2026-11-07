from collections.abc import Callable, Coroutine, Mapping
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass
from functools import cache
from types import new_class
from typing import Any, Literal, cast

from fastapi import Request, Response
from fastapi._compat import ModelField
from fastapi.routing import APIRoute, _effective_route_context_var, _EffectiveRouteContext
from pydantic import BaseModel
from typing_extensions import override

# Scoped to the complete FastAPI handler, including validation and serialization. Error paths reset it too.
_HEAD_RESPONSE: ContextVar[bool] = ContextVar("cadwyn_head_response", default=False)


@dataclass(frozen=True)
class _ResponseInstancePlan:
    kind: Literal["model", "list", "dict", "nullable", "union"]
    model: type[BaseModel] | None = None
    children: tuple["_ResponseInstancePlan", ...] = ()


@cache
def _response_instance_plan(field: ModelField) -> _ResponseInstancePlan | None:
    # Core schemas are an untyped FastAPI/Pydantic boundary. Inspect them without invoking user validators.
    return _make_response_instance_plan(field._type_adapter.core_schema, {}, frozenset())


def _make_response_instance_plan(
    schema: Mapping[str, Any], definitions: Mapping[str, Any], visiting: frozenset[int]
) -> _ResponseInstancePlan | None:
    if id(schema) in visiting:
        return None
    visiting = visiting | {id(schema)}
    kind = schema["type"]
    if kind == "definitions":
        definitions = dict(definitions) | {definition["ref"]: definition for definition in schema["definitions"]}
        return _make_response_instance_plan(schema["schema"], definitions, visiting)
    if kind == "definition-ref":
        return _make_response_instance_plan(definitions[schema["schema_ref"]], definitions, visiting)
    if kind in {"function-after", "default"}:
        return _make_response_instance_plan(schema["schema"], definitions, visiting)
    if kind == "model":
        model = schema["cls"]
        if (
            isinstance(model, type)
            and issubclass(model, BaseModel)
            and schema.get("config", {}).get("revalidate_instances") != "always"
        ):
            return _ResponseInstancePlan("model", model=model)
        return None
    if kind in {"list", "dict", "nullable"}:
        if kind == "dict" and schema.get("keys_schema", {}).get("type") != "str":
            return None
        child_schema = (
            schema["schema"]
            if kind == "nullable"
            else schema.get("items_schema" if kind == "list" else "values_schema", {"type": "any"})
        )
        child = _make_response_instance_plan(child_schema, definitions, visiting)
        return _ResponseInstancePlan(kind, children=(child,)) if child is not None else None
    if kind == "union":
        # A left-to-right union can keep only the first choice without trying another model's validation first.
        schemas = schema["choices"][:1] if schema.get("mode", "smart") == "left_to_right" else schema["choices"]
        choices = [
            _make_response_instance_plan(choice[0] if isinstance(choice, tuple) else choice, definitions, visiting)
            for choice in schemas
        ]
        if all(choice is not None for choice in choices):
            return _ResponseInstancePlan("union", children=tuple(choice for choice in choices if choice is not None))
    # Before/wrap/plain validators, discriminators and other schemas can choose or replace the model themselves.
    return None


def _matches_response_instances(plan: _ResponseInstancePlan, value: Any) -> tuple[bool, bool]:
    """Whether all model positions keep their instances, and whether there is at least one such instance.

    Mixed or uncertain containers use the existing preparation as a whole. In particular we never preserve a
    different model class, a subclass, a dict standing in for a model, or an ORM object.
    """
    if plan.kind == "model":
        matches = type(value) is plan.model
        return matches, matches
    if plan.kind == "nullable":
        return (True, False) if value is None else _matches_response_instances(plan.children[0], value)
    if plan.kind == "union":
        return next(
            (match for child in plan.children if (match := _matches_response_instances(child, value))[0]),
            (False, False),
        )
    if plan.kind == "list" and type(value) is list:
        matches = [_matches_response_instances(plan.children[0], item) for item in value]
    elif plan.kind == "dict" and type(value) is dict and all(type(key) is str for key in value):
        matches = [_matches_response_instances(plan.children[0], item) for item in value.values()]
    else:
        return False, False
    return all(match[0] for match in matches), any(match[1] for match in matches)


def _preserve_response_instances(field: ModelField, value: Any) -> bool:
    plan = _response_instance_plan(field)
    matches, has_instances = _matches_response_instances(plan, value) if plan is not None else (False, False)
    preserve = matches and has_instances
    _HEAD_RESPONSE.set(preserve)
    return preserve


class _ResponseModelField(ModelField):
    def __init__(self, head: ModelField, versioned: ModelField) -> None:
        # Reuse the built field instead of constructing a TypeAdapter and running users' schema hooks again.
        self.__dict__.update(vars(versioned))
        self._head = head
        self._versioned = versioned

    def _field(self) -> ModelField:
        return self._head if _HEAD_RESPONSE.get() else self._versioned

    @override
    def validate(
        self, value: Any, values: dict[str, Any] | None = None, *, loc: tuple[int | str, ...] = ()
    ) -> tuple[Any, list[dict[str, Any]]]:
        return self._field().validate(value, values or {}, loc=loc)

    @override
    def serialize(self, value: Any, **kwargs: Any) -> Any:
        return self._field().serialize(value, **kwargs)

    @override
    def serialize_json(self, value: Any, **kwargs: Any) -> bytes:
        return self._field().serialize_json(value, **kwargs)


class _HeadResponseRoute(APIRoute):
    """Select the runtime response field while keeping the versioned field for OpenAPI."""

    _runtime_response_field: ModelField

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        context = _effective_route_context_var.get()
        if context is not None and context.original_route is self:
            context = copy(context)
        else:
            context = _EffectiveRouteContext(original_route=self)
            for name in vars(context):
                if name != "original_route" and hasattr(self, name):
                    setattr(context, name, getattr(self, name))
        context.response_field = self._runtime_response_field
        token = _effective_route_context_var.set(context)
        try:
            handler = super().get_route_handler()
        finally:
            _effective_route_context_var.reset(token)

        async def handle(request: Request) -> Response:
            token = _HEAD_RESPONSE.set(False)
            try:
                return await handler(request)
            finally:
                _HEAD_RESPONSE.reset(token)

        return handle


def _use_head_response_model(route: APIRoute, head: ModelField, versioned: ModelField) -> None:
    route.__class__ = new_class(
        f"Cadwyn{type(route).__name__}",
        (_HeadResponseRoute, type(route)),  # ty: ignore[unsupported-dynamic-base]
    )
    cast("_HeadResponseRoute", route)._runtime_response_field = _ResponseModelField(head, versioned)
