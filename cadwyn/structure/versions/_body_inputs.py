import dataclasses
import functools
from typing import (
    Any,
    TypeAlias,
    Union,
    cast,
)

from fastapi._compat import (
    ModelField,
)
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel, RootModel

from cadwyn._utils import (
    is_same_value,
)

from ._core_schema import (
    _CORE_SCHEMA_DEFINITIONS,
    _CORE_SCHEMA_ERRORS,
    _UNION_SCHEMA_TYPES,
    _do_compiled_aliases_match,
    _get_model_inner_core_schema,
    _get_schema_shape,
    _get_validation_paths,
    _is_custom_serialization,
    _register_core_schema_definitions,
)
from ._dependencies import _get_dependency_overrides, _iter_dependants_that_fastapi_solves

# How the head validates the data of a body schema, as far as the models of the request's version in it go:
# `("model", cls, head_cls)` for a model of the request's version and the head's model at the same place,
# `("items", plan)` for the items of a list, `("values", plan)` for the values of a dict, and `("union", plans)` for
# the members of a union. None means that the data stays as it is.
_InputPlan: TypeAlias = Union[tuple[Any, ...], None]
# The plans of the pairs of a body field of a version and the head's body field, by the ids of the fields
_BODY_INPUT_PLANS: dict[tuple[int, int], tuple[ModelField, ModelField, _InputPlan]] = {}


@dataclasses.dataclass(frozen=True)
class _ModelInputPlan:
    # (name, key, plan) of the fields: the key that the head validates the field from, and the plan of its value
    fields: tuple[tuple[str, str, _InputPlan], ...] = ()
    # The keys of the data that any field validates, which the extra keys must not have
    validated_keys: frozenset[Union[str, int]] = frozenset()


def _to_head_input(
    route: APIRoute, head_dependant: Dependant, head_route: APIRoute, body: Any, *, embed_body_fields: bool
) -> Any:
    """Return the migrated body with the models of the request's version in it replaced by the data of their fields.

    Bodies that aren't models themselves, such as `list[Model]`, reach migrations as FastAPI validates them, with
    the models of the request's version in them. FastAPI validates such models for the head with
    `from_attributes=True`, which only reads the attributes of their fields by their validation aliases. So the head
    would lose their extra keys, take all of their fields as set, and miss the fields with aliases.

    So we give the head the data that the client has sent instead: the values of the fields that are set, under the
    keys that the head validates them from, and the extra keys. We take the values as they are, and only walk into
    the places where both the body's schema and the head's have models, through lists, dicts, and unions without
    validators or serializers in either of them. Everything else, e.g. the values of `Any` fields, the models that
    a validator returns, the models of the head that a migration adds, tuples, sets, and root models, stays as it is.
    We only copy the lists and dicts in which something changes.

    Every param of the head's dependencies that reads the body gets the same data, so we only change it if all of them
    validate it by the same plan as the endpoint's body param.
    """
    if embed_body_fields or len(route.dependant.body_params) != 1 or len(head_dependant.body_params) != 1:
        return body
    body_field = route.dependant.body_params[0]
    plan = _get_body_input_plan(body_field, head_dependant.body_params[0])
    if plan is None or not _do_body_readers_share_plan(body_field, head_dependant, head_route, plan):
        return body
    # Recursive schemas let the data be as deep as pydantic accepts. Nothing here has side effects, so we can just
    # leave such a body as it is.
    try:
        return _to_input_by_plan(body, plan)
    except RecursionError:  # pragma: no cover # Python 3.12+ raises it only for bodies that pydantic rejects
        return body


def _do_body_readers_share_plan(
    body_field: ModelField, head_dependant: Dependant, head_route: APIRoute, plan: _InputPlan
) -> bool:
    """Whether every param in the head's dependency tree that reads the body validates it by the plan.

    If the app has dependency overrides, FastAPI builds the dependants of all dependencies again on each request, from
    what they are then (e.g. their signatures), which runs code of users (e.g. the schema hooks of their types). We
    would have to build them once more to check them, so we leave the body as it is in such apps. Without overrides,
    FastAPI solves the dependants that it has built for the route, which are the ones that we check.
    """
    if _get_dependency_overrides(head_route):
        return False
    return all(
        _get_body_input_plan(body_field, reader_field) == plan
        for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
        for reader_field in dependant.body_params
    )


def _get_body_input_plan(body_field: ModelField, head_body_field: ModelField) -> _InputPlan:
    cached = _BODY_INPUT_PLANS.get((id(body_field), id(head_body_field)))
    if cached is not None and cached[0] is body_field and cached[1] is head_body_field:
        return cached[2]
    try:
        plan = _get_input_plan(
            _register_core_schema_definitions(cast("dict[str, Any]", body_field._type_adapter.core_schema)),
            _register_core_schema_definitions(cast("dict[str, Any]", head_body_field._type_adapter.core_schema)),
        )
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas
        plan = None
    _BODY_INPUT_PLANS[id(body_field), id(head_body_field)] = (body_field, head_body_field, plan)
    return plan


def _get_input_plan(
    schema: dict[str, Any],
    head_schema: dict[str, Any],
    visiting: frozenset[str] = frozenset(),
    head_visiting: frozenset[str] = frozenset(),
) -> _InputPlan:
    """Return where the data of the request's version's core schema has models that the head's has at the same place.

    The way to them must have no validators or serializers in either of the schemas.
    """
    unwrapped, head_unwrapped = _unwrap_input_schema(schema, visiting), _unwrap_input_schema(head_schema, head_visiting)
    if unwrapped is None or head_unwrapped is None:
        return None
    (schema, visiting), (head_schema, head_visiting) = unwrapped, head_unwrapped
    if _UNION_SCHEMA_TYPES.intersection((schema["type"], head_schema["type"])):
        return _get_union_input_plan(schema, head_schema, visiting, head_visiting)
    if _get_schema_shape(schema) is None or _get_schema_shape(schema) != _get_schema_shape(head_schema):
        return None
    schema_type = schema["type"]
    if schema_type == "model":
        return "model", schema["cls"], head_schema["cls"]
    child_key = "values_schema" if schema_type == "dict" else "items_schema"
    plan = _get_input_plan(schema[child_key], head_schema[child_key], visiting, head_visiting)
    if plan is None:
        return None
    return ("values", plan) if schema_type == "dict" else ("items", plan)


def _get_union_input_plan(
    schema: dict[str, Any], head_schema: dict[str, Any], visiting: frozenset[str], head_visiting: frozenset[str]
) -> _InputPlan:
    """Return the plans of the members of the request's version's union that have one member of the same shape.

    The members are looked up in the head's union, or are the head's schema itself if it isn't a union.
    """
    head_members = _get_input_union_members(head_schema, head_visiting)
    plans: dict[_InputPlan, None] = {}
    for member, member_visiting in _get_input_union_members(schema, visiting):
        shape = _get_schema_shape(member)
        matching_head_members = [
            head_member
            for head_member in head_members
            if shape is not None and _get_schema_shape(head_member[0]) == shape
        ]
        if len(matching_head_members) != 1:
            continue
        head_member, head_member_visiting = matching_head_members[0]
        plan = _get_input_plan(member, head_member, member_visiting, head_member_visiting)
        for member_plan in plan[1] if plan is not None and plan[0] == "union" else (plan,):
            plans[member_plan] = None
    plans.pop(None, None)
    return ("union", tuple(plans)) if plans else None


def _get_input_union_members(
    schema: dict[str, Any], visiting: frozenset[str]
) -> list[tuple[dict[str, Any], frozenset[str]]]:
    """Return the unwrapped members of a union, including the ones of the unions in it, or the schema itself"""
    if schema["type"] not in _UNION_SCHEMA_TYPES:
        return [(schema, visiting)]
    choices = schema["choices"].values() if schema["type"] == "tagged-union" else schema["choices"]
    members: list[tuple[dict[str, Any], frozenset[str]]] = []
    for choice in choices:
        # Union choices can be `(schema, label)` tuples
        unwrapped = _unwrap_input_schema(choice[0] if isinstance(choice, tuple) else choice, visiting)
        if unwrapped is not None:
            members.extend(_get_input_union_members(*unwrapped))
    return members


def _unwrap_input_schema(
    schema: dict[str, Any], visiting: frozenset[str]
) -> Union[tuple[dict[str, Any], frozenset[str]], None]:
    """Return the schema without the references, optional values, and defaults around it, or None if it's unsafe.

    The own core schema of a model becomes the model schema itself.
    """
    while True:
        # References carry the serializers of `Annotated[Model, PlainSerializer(...)]`
        if _is_custom_serialization(schema.get("serialization")):
            return None
        if schema["type"] == "definition-ref":
            ref = schema["schema_ref"]
            # A recursive type alias of containers, e.g. `type Items = list[Union[Item, Items]]`
            if ref in visiting:
                return None
            visiting |= {ref}
            schema = _CORE_SCHEMA_DEFINITIONS[ref]
            continue
        # The own core schema of a model has its ref, and wraps it in the "after" validators of the model
        if schema.get("ref") is not None:
            model_schema = schema
            while model_schema["type"] == "function-after":
                model_schema = model_schema["schema"]
            if model_schema["type"] == "model":
                return model_schema, visiting
        if schema["type"] == "nullable" or (schema["type"] == "default" and schema.get("on_error", "raise") == "raise"):
            schema = schema["schema"]
            continue
        return schema, visiting


def _to_input_by_plan(data: Any, plan: _InputPlan) -> Any:
    if plan is None:
        return data
    kind = plan[0]
    if kind == "union":
        # We only walk into the member that the data surely belongs to
        matching_plans = [member for member in plan[1] if _plan_matches(member, data)]
        return _to_input_by_plan(data, matching_plans[0]) if len(matching_plans) == 1 else data
    if not _plan_matches(plan, data):
        return data
    if kind == "model":
        model_plan = _get_model_input_plan(plan[1], plan[2])
        return data if model_plan is None else _model_to_input(data, model_plan)
    if kind == "values":
        values = {key: _to_input_by_plan(value, plan[1]) for key, value in data.items()}
        has_changed = any(values[key] is not value for key, value in data.items())
        return values if has_changed else data
    items = [_to_input_by_plan(item, plan[1]) for item in data]
    return items if any(item is not original for item, original in zip(items, data, strict=True)) else data


def _plan_matches(plan: tuple[Any, ...], data: Any) -> bool:
    """Whether the data has the exact type that the plan's schema validates data into"""
    kind = plan[0]
    if kind == "model":
        return type(data) is plan[1]
    return type(data) is (list if kind == "items" else dict)


def _model_to_input(model: BaseModel, plan: _ModelInputPlan) -> Any:
    extra = model.__pydantic_extra__
    # The head would take an extra key that a field validates as the field's value
    if extra and not plan.validated_keys.isdisjoint(extra):
        return model
    fields_set = model.model_fields_set
    data = {
        key: _to_input_by_plan(getattr(model, name), field_plan)
        for name, key, field_plan in plan.fields
        if name in fields_set
    }
    if extra:
        data.update(extra)
    return data


@functools.cache
def _get_model_input_plan(model: type[BaseModel], head_model: type[BaseModel]) -> Union[_ModelInputPlan, None]:
    """Return how the head validates the data of the fields of a model of the request's version, if it surely does.

    Only copies of the head's models need it, because FastAPI takes the head's own models as they are. A copy must
    have the fields of the head's model at the same place with their keys and defaults: the head reads the attributes
    of the fields that it has, which keeps the defaults of the request's version and ignores the fields that only the
    request's version has, while it would get such fields from the data as extra keys. Both models must validate the
    data of their fields as a whole, without validators that get the raw input (e.g. `model_validator(mode="before")`)
    or a custom `__init__`, and we only walk into the values of fields whose schemas have models in both of them.
    Root models aren't converted at all: their validators get their data as a whole (e.g. a `mode="before"` validator
    of `RootModel[int]` gets the int), which they might transform once more.

    Every field gets the key that it's dumped to if the model validates it. A required field that the model validates
    from another key (e.g. `Field(alias="x", validation_alias="v")`) gets its first plain validation key, because the
    head surely rejects the data without it. The head might silently lose the values of other fields, so we leave
    their models as they are. Each key must belong to exactly one field, so that no field takes the value of another.
    """
    if (
        not hasattr(model, "__cadwyn_original_model__")
        # The head's model is a root model too, because both are versions of the same model
        or issubclass(model, RootModel)
        or not _has_fields_of_head_model(model, head_model)
    ):
        return None
    try:
        inner_schema, head_inner_schema = _get_model_inner_core_schema(model), _get_model_inner_core_schema(head_model)
        if inner_schema is None or head_inner_schema is None:
            return None
        field_schemas, head_field_schemas = inner_schema["fields"], head_inner_schema["fields"]
        # A custom `__get_pydantic_core_schema__` can validate other fields or keys than the field infos say
        if not _do_compiled_fields_match(model, field_schemas) or not _do_compiled_fields_match(  # pragma: no cover
            head_model, head_field_schemas
        ):
            return None
        fields: list[tuple[str, str, _InputPlan]] = []
        keys_of_fields: dict[str, frozenset[Union[str, int]]] = {}
        for name, field in model.model_fields.items():
            paths = _get_validation_paths(model, name, field)
            dumped_key = field.serialization_alias or name
            plain_keys = [path[0] for path in paths if len(path) == 1 and isinstance(path[0], str)]
            if [dumped_key] in paths:
                key = dumped_key
            elif field.is_required() and plain_keys:
                key = plain_keys[0]
            else:
                return None
            keys_of_fields[name] = frozenset(path[0] for path in paths)
            plan = _get_input_plan(field_schemas[name]["schema"], head_field_schemas[name]["schema"])
            fields.append((name, key, plan))
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas
        return None
    if any(
        key in other_keys
        for name, key, _ in fields
        for other_name, other_keys in keys_of_fields.items()
        if other_name != name
    ):
        return None
    return _ModelInputPlan(fields=tuple(fields), validated_keys=frozenset().union(*keys_of_fields.values()))


def _do_compiled_fields_match(model: type[BaseModel], field_schemas: dict[str, Any]) -> bool:
    return set(field_schemas) == set(model.model_fields) and all(
        _do_compiled_aliases_match(field, field_schemas[name]) for name, field in model.model_fields.items()
    )


def _has_fields_of_head_model(model: type[BaseModel], head_model: type[BaseModel]) -> bool:
    """Whether a copy of a model has the fields of the head's model, with the same keys and defaults"""
    if model.model_fields.keys() != head_model.model_fields.keys():
        return False
    for name, field in model.model_fields.items():
        head_field = head_model.model_fields[name]
        if (
            (field.serialization_alias or name) != (head_field.serialization_alias or name)
            or _get_validation_paths(model, name, field) != _get_validation_paths(head_model, name, head_field)
            or _unwrap_default_factory(field.default_factory) is not _unwrap_default_factory(head_field.default_factory)
            # Defaults can be equal but different, e.g. `Decimal("1.00")` and `Decimal("1.0")`
            or not is_same_value(field.default, head_field.default)
        ):
            return False
    return True


def _unwrap_default_factory(factory: object) -> object:
    # Copies of models wrap the default factories of the head's models. `schema_generation` imports this module.
    from cadwyn.schema_generation import _CallableWrapper

    return factory._original_callable if isinstance(factory, _CallableWrapper) else factory
