import dataclasses
import functools
import uuid
from collections.abc import (
    Callable,
)
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import (
    TYPE_CHECKING,
    Any,
    TypeAlias,
    Union,
    cast,
)

from pydantic import BaseModel, RootModel
from pydantic_core import (
    PydanticSerializationError,
    TzInfo,
    from_json,
    to_json,
)

from ._core_schema import (
    _CORE_SCHEMA_DEFINITIONS,
    _CORE_SCHEMA_ERRORS,
    _do_compiled_aliases_match,
    _get_dumped_key,
    _get_unchanging_model_core_schema,
    _get_validation_paths,
    _get_validation_settings,
    _has_core_schema_of_types,
    _is_custom_serialization,
    _register_core_schema_definitions,
)

if TYPE_CHECKING:
    from fastapi._compat import ModelField
    from fastapi.dependencies.models import Dependant
    from fastapi.routing import APIRoute
else:
    from cadwyn_core._stand_ins import (  # noqa: TC001
        APIRoute,
        Dependant,
        ModelField,
    )


def _get_route_json_values_plan(
    head_dependant: "Dependant", route: "APIRoute", *, embed_body_fields: bool
) -> "Union[_JsonValuesPlan, None]":
    if embed_body_fields or len(head_dependant.body_params) != 1 or len(route.dependant.body_params) != 1:
        return None
    return _get_json_values_plan(head_dependant.body_params[0], route.dependant.body_params[0])


@dataclasses.dataclass(slots=True)
class _ClientJsonStrings:
    """The strings that the client has sent in `Json[...]` fields, by their paths in the body.

    We put new copies of them into the body, which nothing else can have. Python shares some strings, though, e.g.
    the empty string, and some versions of pydantic decode strings of one character into the ones that Python
    shares, so we can't copy them. A migration might set the same string objects, so we only treat them as the
    client's if no migration has run, and then nothing can have changed them.
    """

    by_path: "dict[_JsonPath, str]" = dataclasses.field(default_factory=dict)
    shared_paths: "set[_JsonPath]" = dataclasses.field(default_factory=set)

    def forget_shared_strings(self) -> None:
        for path in self.shared_paths:
            del self.by_path[path]


def _copy_decoded_json_strings(plan: "Union[_JsonValuesPlan, None]", body: Any, strings: _ClientJsonStrings) -> Any:
    """Return the body of the request's version with new copies of the strings that it has decoded from JSON.

    The head would decode these strings once more, so we encode them, e.g. `"123"` as `'"123"'`. But a string that
    a migration sets might be JSON already (e.g. one that it has encoded to work around the 500s of `Json[...]`
    fields before 8.2.0), so we remember the copies that we put into the body by their paths in `strings`, and only
    encode a string if it is the very copy that we have put at its path. Python and pydantic share other strings
    (e.g. `"5"`, or the strings that pydantic caches as it decodes JSON) with whatever code makes the same ones,
    and a migration can still get a copy, e.g. with `sys.intern()`, but not put it at its path by accident.
    It skips the containers that it doesn't expect, e.g. in a default that FastAPI gives without validating it.
    """
    if plan is None:
        return body
    return _replace_planned_json_values(
        plan, body, functools.partial(_copy_decoded_json_string, strings=strings), (), is_strict=False
    )


def _copy_decoded_json_string(_check: Any, value: Any, path: "_JsonPath", *, strings: _ClientJsonStrings) -> Any:
    if type(value) is not str:
        return value
    if len(value) > 1:
        copy = value[:1] + value[1:]
    elif value and ord(value) > 0xFF:
        copy = chr(ord(value))
    else:
        copy = from_json(to_json(value), cache_strings=False)
        if copy is value or (value and copy is chr(ord(value))):
            strings.by_path[path] = value
            strings.shared_paths.add(path)
            return value
    strings.by_path[path] = copy
    return copy


def _encode_planned_json_values(
    plan: "_JsonValuesPlan",
    body: Any,
    client_json_strings: _ClientJsonStrings,
    has_other_body_readers: Callable[[], bool],
) -> Any:
    """Return a copy of the migrated body with the values of the plan encoded as JSON, or the body as it is.

    `_encode_json_values` describes when we encode them. `has_other_body_readers` tells whether anything besides
    the body param reads the body, which would get the encoded values too.
    """
    client_strings = client_json_strings.by_path
    try:
        try:
            client_strings_check = None
            has_values_to_encode = _has_json_values_to_encode(
                plan, body, functools.partial(_is_json_value_to_encode, strings=client_strings), ()
            )
        except _UnsafeJsonValueError:
            if client_json_strings.shared_paths:
                raise
            client_strings_check = _ClientJsonStringsCheck(client_strings)
            has_values_to_encode = _has_json_values_to_encode(plan, body, client_strings_check, ())
            client_strings_check.check_that_all_are_in_place()
        if not has_values_to_encode or has_other_body_readers():
            return body
        if client_strings_check is not None:
            client_strings_check = _ClientJsonStringsCheck(client_strings)
            encoded = _replace_planned_json_values(
                plan,
                body,
                lambda check, value, path: (
                    _encode_json_value(value) if client_strings_check(check, value, path) else value
                ),
                (),
                is_strict=True,
            )
            client_strings_check.check_that_all_are_in_place()
            return encoded
        return _replace_planned_json_values(
            plan,
            body,
            lambda check, value, path: (
                _encode_json_value(value)
                if _is_json_value_to_encode(check, value, path, strings=client_strings)
                else value
            ),
            (),
            is_strict=True,
        )
    except Exception:  # noqa: BLE001
        return body


class _UnsafeJsonValueError(Exception):
    """The head might not get what the client has sent if we encode the `Json[...]` values of the body"""


_JsonValuesPlan: TypeAlias = tuple[Any, ...]
_JsonPath: TypeAlias = tuple[Any, ...]
_JSON_CHILD_SCHEMA_KEYS = {
    "nullable": ("schema", "nullable"),
    "list": ("items_schema", "items"),
    "dict": ("values_schema", "values"),
}
_JsonPlanSource: TypeAlias = "Union[ModelField, type[BaseModel]]"
_BODY_FIELD_JSON_PLANS: dict[int, tuple[_JsonPlanSource, Union[_JsonValuesPlan, None]]] = {}
_NORMALIZED_CORE_SCHEMAS: dict[int, tuple[_JsonPlanSource, Any]] = {}
_JSON_VALUES_PLANS: dict[tuple[int, int], tuple[_JsonPlanSource, _JsonPlanSource, Union[_JsonValuesPlan, None]]] = {}
_JsonPlanMemo: TypeAlias = dict[tuple[bool, type[BaseModel], int], Any]
_UNSAFE_JSON_VALUE = object()
_MAX_JSON_PLAN_DEPTH = 8
_JSON_LEAF_VALUE_TYPES: dict[str, tuple[type, ...]] = {
    "none": (type(None),),
    "bool": (bool,),
    "int": (int,),
    "float": (float,),
    "decimal": (Decimal,),
    "str": (str,),
    "date": (date,),
    "time": (time,),
    "datetime": (datetime,),
    "timedelta": (timedelta,),
    "uuid": (uuid.UUID,),
}
_JSON_LEAF_SCHEMA_TYPES = frozenset((*_JSON_LEAF_VALUE_TYPES, "any", "literal", "enum"))
_JSON_NATIVE_TYPES = (str, int, float, bool, type(None))
_PLAIN_JSON_SCHEMA_TYPES = frozenset(
    ("any", "none", "bool", "int", "float", "str", "literal", *_JSON_CHILD_SCHEMA_KEYS)
)
_PLAIN_LITERAL_TYPES = (str, int, bool, type(None))
_PLAIN_JSON_SCHEMA_KEYS = frozenset(
    ("type", "metadata", "expected", "schema", "items_schema", "keys_schema", "values_schema")
)
_STRING_CONFIG_KEYS = (
    *("str_to_lower", "str_to_upper", "str_strip_whitespace", "str_min_length", "str_max_length"),
    "coerce_numbers_to_str",
)
_DETERMINISTIC_DEFAULT_FACTORIES = (list, dict, set, frozenset, tuple)


def _get_json_values_plan(
    head_body_field: _JsonPlanSource, body_field: _JsonPlanSource
) -> Union[_JsonValuesPlan, None]:
    """Return what to encode in the dump of the body field of the request's version, which the head validates.

    The request's version dumps the body, so its models must dump the values to the same keys that the head reads
    them from. Otherwise, e.g. if an older version excludes a field from the dump or dumps another field to its key,
    the head wouldn't get what the client has sent, so we leave the body as it is.
    """
    cached = _JSON_VALUES_PLANS.get((id(head_body_field), id(body_field)))
    if cached is not None and cached[0] is head_body_field and cached[1] is body_field:
        return cached[2]
    try:
        plan = _get_body_field_json_plan(head_body_field)
        if (
            plan is not None
            and body_field is not head_body_field
            and _get_normalized_core_schema(body_field) != _get_normalized_core_schema(head_body_field)
        ):
            plan = None
    except Exception:  # noqa: BLE001
        plan = None
    _JSON_VALUES_PLANS[id(head_body_field), id(body_field)] = (head_body_field, body_field, plan)
    return plan


def _get_core_schema(source: _JsonPlanSource) -> Any:
    if isinstance(source, type) and issubclass(source, BaseModel):
        return source.__pydantic_core_schema__
    return source._type_adapter.core_schema


def _get_body_field_json_plan(body_field: _JsonPlanSource) -> Union[_JsonValuesPlan, None]:
    cached = _BODY_FIELD_JSON_PLANS.get(id(body_field))
    if cached is not None and cached[0] is body_field:
        return cached[1]
    try:
        schema = cast("dict[str, Any]", _get_core_schema(body_field))
        description = (
            _walk_json_values(_register_core_schema_definitions(schema), frozenset(), {}, is_in_json=False)
            if _has_core_schema_of_types(schema, frozenset(("json",)))
            else None
        )
    except (_UnsafeJsonValueError, *_CORE_SCHEMA_ERRORS):
        description = None
    plan = _prune_json_values_description(description, {})
    _BODY_FIELD_JSON_PLANS[id(body_field)] = (body_field, plan)
    return plan


def _get_normalized_core_schema(body_field: _JsonPlanSource) -> Any:
    cached = _NORMALIZED_CORE_SCHEMAS.get(id(body_field))
    if cached is None or cached[0] is not body_field:
        cached = (body_field, _normalize_core_schema(_get_core_schema(body_field), {}))
        _NORMALIZED_CORE_SCHEMAS[id(body_field)] = cached
    return cached[1]


def _normalize_core_schema(value: Any, refs: dict[str, int]) -> Any:
    """Return the core schema with its classes by their names and its definitions by the order of their references.

    Every other value is tagged with its type, because values of different types can be equal in Python (e.g. `1`,
    `1.0`, and `True`) while pydantic tells them apart, e.g. in literals and defaults. Metadata (e.g. the functions
    that make JSON schemas) doesn't change how the schema validates.
    """
    if isinstance(value, dict):
        return {
            _normalize_core_schema(key, refs): refs.setdefault(item, len(refs))
            if key in ("ref", "schema_ref") and isinstance(item, str)
            else _normalize_core_schema(item, refs)
            for key, item in value.items()
            if key != "metadata"
        }
    if isinstance(value, (list, tuple)):
        return type(value).__name__, tuple(_normalize_core_schema(item, refs) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value).__name__, frozenset(_normalize_core_schema(item, refs) for item in value)
    if isinstance(value, type):
        return "class", value.__module__, value.__qualname__
    return "value", type(value).__module__, type(value).__qualname__, value


def _prune_json_values_description(
    description: Union[_JsonValuesPlan, None], memo: dict[int, Union[_JsonValuesPlan, None]]
) -> Union[_JsonValuesPlan, None]:
    """Return the plan of what to encode: the parts of the description that lead to `Json[...]` values.

    The descriptions of models are shared by the places that have them, so we prune each of them once.
    """
    if description is None:
        return None
    if id(description) in memo:
        return memo[id(description)]
    if description[0] == "json":
        plan = description
    elif description[0] == "model":
        fields = tuple(
            (key, child_plan)
            for key, child in description[2]
            if (child_plan := _prune_json_values_description(child, memo)) is not None
        )
        plan = ("model", description[1], fields) if fields else None
    else:
        child = _prune_json_values_description(description[1], memo)
        plan = None if child is None else (description[0], child)
    memo[id(description)] = plan
    return plan


def _walk_json_values(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    """Describe how the head reads the dumped data of the core schema, or raise `_UnsafeJsonValueError`.

    We only encode the `Json[...]` values of a body if the head gets what the client has sent everywhere in it,
    so we raise for anything that might change or drop a value: validators (the dump has already been validated,
    so the head would change it once more), serializers, defaults that swallow errors or that the head computes once
    more, and the data of models that we can't check. In `Json[...]` values, the head reads JSON that we encode,
    so we also raise for anything whose JSON might differ from what the client has sent, e.g. some unions.
    """
    if _is_custom_serialization(schema.get("serialization")) or schema.get("on_error", "raise") != "raise":
        raise _UnsafeJsonValueError
    schema_type = schema["type"]
    if schema_type in _JSON_LEAF_SCHEMA_TYPES:
        return _walk_json_leaf(schema, is_in_json=is_in_json)
    if schema_type == "default":
        if "default_factory" in schema and schema["default_factory"] not in _DETERMINISTIC_DEFAULT_FACTORIES:
            raise _UnsafeJsonValueError
        return _walk_json_values(schema["schema"], visiting, memo, is_in_json=is_in_json)
    walk = _JSON_SCHEMA_WALKERS.get(schema_type)
    if walk is not None:
        return walk(schema, visiting, memo, is_in_json=is_in_json)
    if schema_type not in _JSON_CHILD_SCHEMA_KEYS:
        raise _UnsafeJsonValueError
    child_key, kind = _JSON_CHILD_SCHEMA_KEYS[schema_type]
    if not is_in_json:
        if "keys_schema" in schema and _walk_json_values(schema["keys_schema"], visiting, memo, is_in_json=False):
            raise _UnsafeJsonValueError
        child = _walk_json_values(schema[child_key], visiting, memo, is_in_json=False) if child_key in schema else None
        return None if child is None else (kind, child)
    child = _walk_json_values(schema[child_key], visiting, memo, is_in_json=True) if child_key in schema else ("any",)
    if schema_type != "dict":
        return kind, child
    keys_check = (
        _walk_json_values(schema["keys_schema"], visiting, memo, is_in_json=True)
        if "keys_schema" in schema
        else ("any",)
    )
    if keys_check[0] not in ("leaf", "enum", "any") or (keys_check[0] != "any" and len(keys_check[-1]) != 1):
        raise _UnsafeJsonValueError
    return kind, child, keys_check


def _walk_json_leaf(schema: dict[str, Any], *, is_in_json: bool) -> Union[_JsonValuesPlan, None]:
    """Describe the exact types of the values of a leaf in a `Json[...]` value, so that we can check them.

    A migration can set values of other types, which encoding might change, e.g. a decimal that becomes a string.
    Outside of `Json[...]` values, the head validates the dumped values themselves, as it does without `Json[...]`.
    """
    schema_type = schema["type"]
    if schema_type == "enum":
        values = [member.value for member in schema["members"]]
        if "missing" in schema or any(type(value) not in _JSON_NATIVE_TYPES for value in values):
            raise _UnsafeJsonValueError
        return ("enum", schema["cls"], frozenset(map(type, values))) if is_in_json else None
    if not is_in_json:
        return None
    if schema_type == "any":
        return ("any",)
    if schema_type == "literal":
        expected = schema["expected"]
        if any(type(value) not in _JSON_NATIVE_TYPES for value in expected) or len(set(expected)) != len(expected):
            raise _UnsafeJsonValueError
        return "leaf", frozenset(map(type, expected))
    return "leaf", frozenset(_JSON_LEAF_VALUE_TYPES[schema_type])


def _walk_json_reference(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    ref = schema["schema_ref"]
    if ref not in visiting:
        return _walk_json_values(_CORE_SCHEMA_DEFINITIONS[ref], visiting | {ref}, memo, is_in_json=is_in_json)
    if is_in_json or _has_core_schema_of_types(_CORE_SCHEMA_DEFINITIONS[ref], frozenset(("json",))):
        raise _UnsafeJsonValueError
    return None


def _walk_json_schema(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    if is_in_json:
        raise _UnsafeJsonValueError
    return "json", (
        _walk_json_values(schema["schema"], frozenset(), memo, is_in_json=True) if "schema" in schema else ("any",)
    )


def _walk_json_union(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Union[_JsonValuesPlan, None]:
    choices = schema["choices"].values() if isinstance(schema["choices"], dict) else schema["choices"]
    choices = [choice[0] if isinstance(choice, tuple) else choice for choice in choices]
    if not all(_is_plain_json_schema(choice) for choice in choices):
        raise _UnsafeJsonValueError
    if not is_in_json:
        return None
    types: set[type] = set()
    for choice in choices:
        check = _walk_json_values(choice, visiting, memo, is_in_json=True)
        if check[0] == "any":
            return check
        if check[0] != "leaf":
            raise _UnsafeJsonValueError
        types.update(check[1])
    return "leaf", frozenset(types)


def _walk_json_tuple(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> None:
    if is_in_json or any(
        _walk_json_values(item, visiting, memo, is_in_json=False) is not None for item in schema["items_schema"]
    ):
        raise _UnsafeJsonValueError


_JSON_SCHEMA_WALKERS: dict[str, Callable[..., Any]] = {
    "definition-ref": _walk_json_reference,
    "json": _walk_json_schema,
    "union": _walk_json_union,
    "tagged-union": _walk_json_union,
    "tuple": _walk_json_tuple,
    "model": lambda schema, visiting, memo, *, is_in_json: _walk_json_model(
        schema["cls"], visiting, memo, is_in_json=is_in_json
    ),
}


def _is_plain_json_schema(schema: dict[str, Any]) -> bool:
    """Whether the core schema only has values that keep their JSON type when we encode them"""
    stack = [schema]
    while stack:
        node = stack.pop()
        if (
            node["type"] not in _PLAIN_JSON_SCHEMA_TYPES
            or not node.keys() <= _PLAIN_JSON_SCHEMA_KEYS
            or (
                node["type"] == "literal" and any(type(value) not in _PLAIN_LITERAL_TYPES for value in node["expected"])
            )
        ):
            return False
        stack.extend(node[key] for key in ("schema", "items_schema", "keys_schema", "values_schema") if key in node)
    return True


def _walk_json_model(
    model: type[BaseModel], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    depth = sum(isinstance(item, type) for item in visiting)
    if model in visiting or depth >= _MAX_JSON_PLAN_DEPTH:
        raise _UnsafeJsonValueError
    key = (is_in_json, model, depth)
    if key not in memo:
        try:
            memo[key] = _describe_json_model(model, visiting | {model}, memo, is_in_json=is_in_json)
        except _UnsafeJsonValueError:
            memo[key] = _UNSAFE_JSON_VALUE
    if memo[key] is _UNSAFE_JSON_VALUE:
        raise _UnsafeJsonValueError
    return memo[key]


def _describe_json_model(
    model: type[BaseModel], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    """Describe the data of a model whose every field the head reads from the key that the field is dumped to.

    Otherwise, the head would give a field its default instead of the client's value, or the value of another field,
    so we raise `_UnsafeJsonValueError`.
    """
    inner_schema = _get_unchanging_model_core_schema(model)
    if inner_schema is None or _get_validation_settings(model)[2] == "allow" or model.model_computed_fields:
        raise _UnsafeJsonValueError
    if any(model.model_config.get(key) for key in _STRING_CONFIG_KEYS) and _has_core_schema_of_types(
        inner_schema, frozenset(("union", "tagged-union"))
    ):
        raise _UnsafeJsonValueError
    if issubclass(model, RootModel):
        return _walk_json_values(inner_schema, visiting, memo, is_in_json=is_in_json)
    dumped_keys = [_get_dumped_key(name, field) for name, field in model.model_fields.items()]
    keys = frozenset(dumped_keys)
    if len(keys) != len(dumped_keys):
        raise _UnsafeJsonValueError
    children = []
    for (name, field), dumped_key in zip(model.model_fields.items(), dumped_keys, strict=True):
        field_schema = inner_schema["fields"][name]
        paths = _get_validation_paths(model, name, field)
        if (
            not _do_compiled_aliases_match(field, field_schema)
            or field_schema.get("serialization_exclude")
            or "serialization_exclude_if" in field_schema
            or [dumped_key] not in paths
            or any(path != [dumped_key] for path in paths if path[0] in keys)
        ):
            raise _UnsafeJsonValueError
        child = _walk_json_values(field_schema["schema"], visiting, memo, is_in_json=is_in_json)
        if child is not None:
            children.append((dumped_key, child))
    return "model", keys, tuple(children)


def _has_json_values_to_encode(
    plan: _JsonValuesPlan,
    data: Any,
    is_to_encode: Callable[[_JsonValuesPlan, Any, "_JsonPath"], bool],
    path: "_JsonPath",
) -> bool:
    """Whether the data has a value to encode, found without changing anything or running code of users.

    It doesn't copy, hash, or compare anything of users (e.g. keys). It walks all of the data, because `is_to_encode`
    raises `_UnsafeJsonValueError` for a value that we can't encode, and it raises for containers that we can't walk
    into too.
    """
    kind = plan[0]
    if kind == "json":
        return is_to_encode(plan[1], data, path)
    if kind == "nullable":
        return data is not None and _has_json_values_to_encode(plan[1], data, is_to_encode, path)
    _is_json_container(kind, data, is_strict=True)
    if kind == "items":
        children = [(plan[1], item, (*path, index)) for index, item in enumerate(data)]
    elif kind == "values":
        children = [(plan[1], item, (*path, _get_json_path_key(key))) for key, item in data.items()]
    else:
        children = [(child, data[key], (*path, key)) for key, child in plan[2] if key in data]
    results = [_has_json_values_to_encode(child, item, is_to_encode, item_path) for child, item, item_path in children]
    return any(results)


def _replace_planned_json_values(
    plan: _JsonValuesPlan,
    data: Any,
    replace: Callable[[_JsonValuesPlan, Any, "_JsonPath"], Any],
    path: "_JsonPath",
    *,
    is_strict: bool,
) -> Any:
    """Replace the values of the plan in the data, copying only the containers in which something changes.

    Checking the dependencies runs code of users (e.g. schema hooks) after `_has_json_values_to_encode`, which might
    change the data, so if `is_strict`, we check the data again as we copy it, and raise `_UnsafeJsonValueError` like
    it does. Otherwise, we skip the containers that we can't walk into.
    """
    kind = plan[0]
    if kind == "json":
        return replace(plan[1], data, path)
    if kind == "nullable":
        return data if data is None else _replace_planned_json_values(plan[1], data, replace, path, is_strict=is_strict)
    if not _is_json_container(kind, data, is_strict=is_strict):
        return data
    if kind == "items":
        items = [
            _replace_planned_json_values(plan[1], item, replace, (*path, index), is_strict=is_strict)
            for index, item in enumerate(data)
        ]
        return data if all(new is old for new, old in zip(items, data, strict=True)) else items
    if kind == "values":
        values = {
            key: _replace_planned_json_values(
                plan[1], value, replace, (*path, _get_json_path_key(key)), is_strict=is_strict
            )
            for key, value in data.items()
        }
        return data if all(values[key] is value for key, value in data.items()) else values
    replaced = data
    for key, field_plan in plan[2]:
        if (
            key in data
            and (
                value := _replace_planned_json_values(field_plan, data[key], replace, (*path, key), is_strict=is_strict)
            )
            is not data[key]
        ):
            if replaced is data:
                replaced = dict(data)
            replaced[key] = value
    return replaced


def _get_json_path_key(key: Any) -> tuple[type, Any]:
    return type(key), key


def _is_json_value_to_encode(
    check: _JsonValuesPlan, value: Any, path: "_JsonPath", *, strings: "dict[_JsonPath, str]"
) -> bool:
    """Whether to encode the value of a `Json[...]` field, or raise `_UnsafeJsonValueError` if we can't encode it"""
    if value is None:
        return False
    if (type(value) is str and not _is_client_json_string(value, path, strings)) or not _passes_json_value_check(
        check, value
    ):
        raise _UnsafeJsonValueError
    return True


def _is_client_json_string(value: Any, path: "_JsonPath", strings: "dict[_JsonPath, str]") -> bool:
    """Whether the value is the copy of a string that the client has sent, which we have put at the path"""
    return type(value) is str and strings.get(path) is value


class _ClientJsonStringsCheck:
    """Says whether a `Json[...]` value is a string that the client has sent, as `_is_client_json_string` does.

    It raises `_UnsafeJsonValueError` if a string that the client has sent is at the place of another value, and
    `check_that_all_are_in_place()` raises it if one of their places isn't in the body anymore, e.g. because
    a migration has moved or removed it. A place that has another value, e.g. JSON that a migration has encoded, is
    fine. The check must see every `Json[...]` value of the body, so walk it strictly.
    """

    def __init__(self, strings: "dict[_JsonPath, str]") -> None:
        self.strings = strings
        self.strings_by_id = {id(string): string for string in strings.values()}
        self.seen_paths: set[_JsonPath] = set()

    def __call__(self, _check: _JsonValuesPlan, value: Any, path: "_JsonPath") -> bool:
        if path in self.strings:
            self.seen_paths.add(path)
        is_client_string = _is_client_json_string(value, path, self.strings)
        if not is_client_string and self.strings_by_id.get(id(value)) is value:
            raise _UnsafeJsonValueError
        return is_client_string

    def check_that_all_are_in_place(self) -> None:
        if len(self.seen_paths) != len(self.strings):
            raise _UnsafeJsonValueError


def _is_json_container(kind: str, data: Any, *, is_strict: bool) -> bool:
    """Whether the data is the container of the kind, as dumps have them, or raise `_UnsafeJsonValueError` if strict.

    Dumps only have lists and dicts. We don't accept other containers, even subclasses (e.g. a tuple or a mapping that
    a migration has put in the data), whose `Json[...]` values we might not encode. Looking keys up and copying dicts
    hashes and compares the keys, which runs the code of users (e.g. the `__eq__` of a subclass of `str` that
    a migration has set, or the `__hash__` of an enum). So the keys of models must be exactly strings, and the keys of
    other dicts exactly the values that JSON has. Iterating over the keys runs nothing of users.
    """
    key_types = (str,) if kind == "model" else _JSON_NATIVE_TYPES
    if kind == "items":
        is_container = type(data) is list
    else:
        is_container = type(data) is dict and all(type(key) in key_types for key in data)
    if not is_container and is_strict:
        raise _UnsafeJsonValueError
    return is_container


def _encode_json_value(value: Any) -> str:
    """Return the value encoded as JSON, or raise `_UnsafeJsonValueError` if we can't encode it"""
    try:
        return to_json(value).decode()
    except PydanticSerializationError as e:
        raise _UnsafeJsonValueError from e


def _is_json_value(value: Any) -> bool:
    """Whether the value only has the values that JSON has, so that it stays what it is when we encode it.

    A container that we've seen already stops the walk, e.g. a list that a migration has put into itself.
    """
    stack = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if type(item) in (dict, list):
            if id(item) in seen:
                return False
            seen.add(id(item))
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                return False
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
        elif type(item) not in _JSON_NATIVE_TYPES or _is_nan(item):
            return False
    return True


def _is_nan(value: Any) -> bool:
    return type(value) is float and value != value


def _is_json_temporal(value: Union[datetime, time]) -> bool:
    """Whether JSON has the datetime or time, e.g. its offset has no seconds, which JSON offsets don't have.

    JSON gives values the time zones of pydantic and no folds, so we don't encode other ones either.
    """
    if value.fold or (value.tzinfo is not None and type(value.tzinfo) is not TzInfo):
        return False
    offset = value.utcoffset()
    return offset is None or offset % timedelta(minutes=1) == timedelta(0)


def _are_json_keys(keys_check: _JsonValuesPlan, value: dict[Any, Any]) -> bool:
    """Whether the keys of the dict pass the check and stay different keys in JSON, whose keys are strings"""
    if keys_check[0] == "any":
        return all(type(key) is str for key in value)
    if not all(_passes_json_value_check(keys_check, key) for key in value):
        return False
    try:
        return len({to_json(key) for key in value}) == len(value)
    except PydanticSerializationError:
        return False


def _passes_json_value_check(check: _JsonValuesPlan, value: Any) -> bool:
    """Whether the head reads all of the value, e.g. a migration hasn't left a key that the head doesn't read"""
    kind = check[0]
    if kind == "any":
        return _is_json_value(value)
    if kind == "leaf":
        return (
            type(value) in check[1]
            and not _is_nan(value)
            and (not isinstance(value, (datetime, time)) or _is_json_temporal(value))
        )
    if kind == "enum":
        return type(value) is check[1] and not _is_nan(value.value)
    if kind == "nullable":
        return value is None or _passes_json_value_check(check[1], value)
    if kind == "items":
        return type(value) is list and all(_passes_json_value_check(check[1], item) for item in value)
    if kind == "values":
        _, values_check, keys_check = check
        return (
            type(value) is dict
            and _are_json_keys(keys_check, value)
            and all(_passes_json_value_check(values_check, item) for item in value.values())
        )
    _, keys, checks = check
    return (
        type(value) is dict
        and all(type(key) is str for key in value)
        and value.keys() <= keys
        and all(key not in value or _passes_json_value_check(key_check, value[key]) for key, key_check in checks)
    )
