from collections import Counter, OrderedDict, defaultdict, deque
from enum import Enum, Flag
from typing import (
    Any,
    Union,
    cast,
)

from fastapi._compat import (
    ModelField,
)
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic_core import (
    SchemaSerializer,
    SchemaValidator,
    core_schema,
)

from cadwyn._utils import (
    get_member_with_same_value,
    is_enum_looked_up_by_value,
)

from ._body_inputs import _do_compiled_fields_match, _InputPlan
from ._core_schema import (
    _CORE_SCHEMA_DEFINITIONS,
    _CORE_SCHEMA_ERRORS,
    _do_compiled_settings_match,
    _get_validation_paths,
    _has_core_schema_of_types,
    _register_core_schema_definitions,
)
from ._dependencies import _get_dependency_overrides, _iter_dependants_that_fastapi_solves


def _replace_with_head_enum_members(data: Any) -> Any:
    """Replace each member of a copy of a plain enum or flag with the member of its head enum that has the same value.

    Python-mode dumps keep the members of the copies, which the head rejects for plain enums and flags. The head looks
    up the members of the copies of `str`, `int`, and `float` enums by their values anyway, so we leave them, as well
    as the members of other enums and the ones that their head enums have no member with exactly the same value for,
    so that the head rejects them as before.

    We only walk into the containers that pydantic makes when it validates a field, and copy only the ones in which
    something below them changes, with the same types. Copying them again hashes their keys and items, which runs code
    of users, so if a copy fails, we keep the container as it is. Anything else, such as models (a dump has none, but
    a migration can put some into the body), iterators, or containers of other types, could run code of users or lose
    what they hold if we iterated or copied it, so we leave it as it is. We walk depth-first without recursion, so that
    data as deep as pydantic accepts never hits the recursion limit, and we don't walk into the containers that contain
    themselves again.
    """
    children = _get_children_that_can_be_enum_members(data)
    if children is None:
        return _replace_with_head_enum_member(data)
    # Each frame is [container, iterator over its children, its children with replaced members, whether any changed]
    stack: list[list[Any]] = [[data, iter(children), [], False]]
    containers_on_stack = {id(data)}
    while True:
        frame = stack[-1]
        child = next(frame[1], _NO_CHILD)
        if child is not _NO_CHILD:
            grandchildren = None if id(child) in containers_on_stack else _get_children_that_can_be_enum_members(child)
            if grandchildren is None:
                replaced_child = _replace_with_head_enum_member(child)
                frame[2].append(replaced_child)
                frame[3] = frame[3] or replaced_child is not child
            else:
                stack.append([child, iter(grandchildren), [], False])
                containers_on_stack.add(id(child))
            continue
        stack.pop()
        container = frame[0]
        containers_on_stack.discard(id(container))
        replaced = _copy_container_with_children(container, frame[2]) if frame[3] else container
        if not stack:
            return replaced
        stack[-1][2].append(replaced)
        stack[-1][3] = stack[-1][3] or replaced is not container


_NO_CHILD = object()
# The containers that pydantic makes when it validates a field (see `GenerateSchema` and `MAPPING_ORIGIN_MAP`)
_MAPPINGS_THAT_PYDANTIC_MAKES = (dict, OrderedDict, defaultdict, Counter)
_COLLECTIONS_THAT_PYDANTIC_MAKES = (list, tuple, set, frozenset, deque)
# Pydantic doesn't export these iterator classes. Obtain their exact types through its public core schema API,
# rather than matching class names or wrapping user iterators (including ones that migrations put into the body).
_PYDANTIC_ITERATOR_TYPES = (
    type(SchemaValidator(core_schema.generator_schema()).validate_python(())),
    type(SchemaSerializer(core_schema.generator_schema()).to_python(iter(()))),
)


def _replace_with_head_enum_member(value: Any) -> Any:
    if not isinstance(value, Enum):
        return value
    head_enum: Union[type[Enum], None] = getattr(type(value), "__cadwyn_original_model__", None)
    if head_enum is None or is_enum_looked_up_by_value(head_enum):
        return value
    head_member = get_member_with_same_value(head_enum, value)
    return value if head_member is None else head_member


def _to_head_lazy_enum_input(
    body: Any, head_dependant: Dependant, head_route: APIRoute, *, enabled: bool, embed_body_fields: bool
) -> Any:
    """Convert lazy members only where every body reader expects the same plain enum.

    An enum inside a union can already succeed through another branch. Walking into that union, or into opaque
    validators or Any, would change working requests. Leave these paths and user iterators as they are.
    """
    if (
        not enabled
        or embed_body_fields
        or len(head_dependant.body_params) != 1
        or _get_dependency_overrides(head_route)
    ):
        return body
    plan = _get_lazy_enum_field_plan(head_dependant.body_params[0])
    if plan is None or any(
        _get_lazy_enum_field_plan(field) != plan
        for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
        for field in dependant.body_params
    ):
        return body
    try:
        return _to_lazy_enum_input_by_plan(body, plan)
    except RecursionError:  # pragma: no cover # Leave bodies deeper than Python's recursion limit unchanged.
        return body


_LAZY_ENUM_INPUT_PLANS: dict[int, tuple[ModelField, _InputPlan]] = {}


def _get_lazy_enum_field_plan(field: ModelField) -> _InputPlan:
    cached = _LAZY_ENUM_INPUT_PLANS.get(id(field))
    if cached is not None and cached[0] is field:
        return cached[1]
    try:
        plan = _get_lazy_enum_input_plan(cast("dict[str, Any]", field._type_adapter.core_schema))
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas.
        plan = None
    _LAZY_ENUM_INPUT_PLANS[id(field)] = field, plan
    return plan


def _get_lazy_enum_input_plan(
    schema: dict[str, Any], *, lazy_item: bool = False, visiting: frozenset[str] = frozenset()
) -> _InputPlan:
    schema = _register_core_schema_definitions(schema)
    kind = schema["type"]
    if kind == "definition-ref":
        ref = schema["schema_ref"]
        if ref in visiting:
            return None
        return _get_lazy_enum_input_plan(_CORE_SCHEMA_DEFINITIONS[ref], lazy_item=lazy_item, visiting=visiting | {ref})
    if kind == "nullable" or (kind == "default" and schema.get("on_error", "raise") == "raise"):
        return _get_lazy_enum_input_plan(schema["schema"], lazy_item=lazy_item, visiting=visiting)
    # Item after-validators only run after the enum accepts the item. A validator surrounding a generator can
    # consume it and catch its errors, so normalization there could change a request that already succeeds.
    if kind == "function-after" and lazy_item and not _has_core_schema_of_types(schema, frozenset(("generator",))):
        return _get_lazy_enum_input_plan(schema["schema"], lazy_item=True, visiting=visiting)
    if kind == "enum" and lazy_item:
        cls = schema["cls"]
        if (
            is_enum_looked_up_by_value(cls)
            or cls._missing_.__func__ not in (Enum._missing_.__func__, Flag._missing_.__func__)
            or cls.__eq__ is not Enum.__eq__
            or cls.__hash__ is not Enum.__hash__
            or any(type(member._value_) not in (str, int, float, bool, type(None)) for member in cls)
        ):
            return None
        return "enum", cls
    if kind in ("generator", "list", "dict"):
        child_key = "values_schema" if kind == "dict" else "items_schema"
        child = _get_lazy_enum_input_plan(
            schema.get(child_key, {"type": "any"}), lazy_item=lazy_item or kind == "generator", visiting=visiting
        )
        return (kind, child) if child is not None else None
    if (
        kind == "model"
        and not schema.get("custom_init")
        and not schema.get("post_init")
        and _do_compiled_settings_match(schema["cls"], schema.get("config"))
    ):
        inner = schema["schema"]
        if schema.get("root_model"):
            return _get_lazy_enum_input_plan(inner, lazy_item=lazy_item, visiting=visiting)
        return _get_lazy_enum_model_plan(schema["cls"], inner, lazy_item=lazy_item, visiting=visiting)
    return None


def _get_lazy_enum_model_plan(
    cls: type[BaseModel], inner: dict[str, Any], *, lazy_item: bool, visiting: frozenset[str]
) -> _InputPlan:
    if inner["type"] != "model-fields" or not _do_compiled_fields_match(cls, inner["fields"]):
        return None
    fields = []
    keys_of_fields = []
    for name, field in cls.model_fields.items():
        paths = _get_validation_paths(cls, name, field)
        if any(len(path) != 1 or type(path[0]) is not str for path in paths):
            return None
        keys = tuple(path[0] for path in paths)
        keys_of_fields.append(set(keys))
        child = _get_lazy_enum_input_plan(inner["fields"][name]["schema"], lazy_item=lazy_item, visiting=visiting)
        if child is not None:
            fields.append((keys, child))
    if any(keys & other for index, keys in enumerate(keys_of_fields) for other in keys_of_fields[index + 1 :]):
        return None
    return ("fields", tuple(fields)) if fields else None


def _to_lazy_enum_input_by_plan(data: Any, plan: tuple[Any, ...]) -> Any:
    kind = plan[0]
    if kind == "enum":
        return (
            _replace_with_head_enum_member(data)
            if getattr(type(data), "__cadwyn_original_model__", None) is plan[1]
            else data
        )
    if kind == "generator":
        return (
            (_to_lazy_enum_input_by_plan(item, plan[1]) for item in data)
            if type(data) in _PYDANTIC_ITERATOR_TYPES
            else data
        )
    if kind == "list" and type(data) is list:
        children = [_to_lazy_enum_input_by_plan(item, plan[1]) for item in data]
        return children if any(old is not new for old, new in zip(data, children, strict=True)) else data
    if kind in ("dict", "fields") and type(data) is dict and all(type(key) is str for key in data):
        copied = data.copy()
        if kind == "dict":
            for key, value in data.items():
                copied[key] = _to_lazy_enum_input_by_plan(value, plan[1])
        else:
            for keys, child in plan[1]:
                for key in keys:
                    if key in data:
                        copied[key] = _to_lazy_enum_input_by_plan(data[key], child)
                        break
        return copied if any(data[key] is not copied[key] for key in data) else data
    return data


def _get_children_that_can_be_enum_members(value: Any) -> Union[list[Any], None]:
    """Return the children of a container, with the keys and values of mappings in turn, or None"""
    if type(value) in _MAPPINGS_THAT_PYDANTIC_MAKES:
        # Unlike the type's own `items` (and iterating an `OrderedDict`), `dict.items` never hashes the keys again
        return [child for item in dict.items(value) for child in item]
    if type(value) in _COLLECTIONS_THAT_PYDANTIC_MAKES:
        return list(value)
    return None


def _copy_container_with_children(container: Any, children: list[Any]) -> Any:
    """Return a copy of the container that has the children instead of its own, or the container if they don't fit"""
    try:
        if type(container) in _MAPPINGS_THAT_PYDANTIC_MAKES:
            items = list(zip(children[::2], children[1::2], strict=True))
            if type(container) is defaultdict:
                copied = defaultdict(container.default_factory, items)
            elif type(container) is Counter:
                copied = Counter(dict(items))
            elif type(container) is OrderedDict:
                # The children have the order of the dict, while `move_to_end` only changes the order of the
                # `OrderedDict`. Iterating it hashes the keys, so we only do it here, where a copy hashes them anyway.
                index_by_key_id = {id(key): index for index, key in enumerate(dict.keys(container))}
                copied = OrderedDict(items[index_by_key_id[id(key)]] for key in container)
            else:
                copied = dict(items)
        elif type(container) is deque:
            copied = deque(children, maxlen=container.maxlen)
        else:
            copied = type(container)(children)
    except Exception:  # noqa: BLE001 # E.g. a key whose `__hash__` fails
        return container
    # Members of the copies don't equal the head members, so two distinct keys or items can become one
    return copied if len(copied) == len(container) else container
