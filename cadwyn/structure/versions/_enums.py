from collections import Counter, OrderedDict, defaultdict, deque
from enum import Enum
from typing import Any, Union, cast

from fastapi._compat import ModelField
from fastapi.routing import APIRoute
from pydantic_core import SchemaSerializer, SchemaValidator, core_schema

from cadwyn._utils import (
    get_member_with_same_value,
    is_enum_looked_up_by_value,
)

from ._core_schema import _CORE_SCHEMA_DEFINITIONS, _register_core_schema_definitions


def _can_lazy_items_be_plain_enum_copies(route: APIRoute) -> bool:
    """Whether the request body of the route can have lazy items that are members of copies of plain enums or flags.

    We only say no when the schema of its single body param is made of nothing but schemas that we know can't make
    such members, and check it once per route, so that the lazy items of other routes are never walked.
    """
    if len(route.dependant.body_params) != 1:
        return True
    field = route.dependant.body_params[0]
    cached = _CAN_LAZY_ITEMS_BE_PLAIN_ENUM_COPIES.get(id(field))
    if cached is not None and cached[0] is field:
        return cached[1]
    result = _can_schema_make_members_of_plain_enum_copies(cast("dict[str, Any]", field._type_adapter.core_schema))
    _CAN_LAZY_ITEMS_BE_PLAIN_ENUM_COPIES[id(field)] = field, result
    return result


_CAN_LAZY_ITEMS_BE_PLAIN_ENUM_COPIES: dict[int, tuple[ModelField, bool]] = {}
# The core schemas that only make values of builtin types
_SCHEMAS_OF_BUILTIN_VALUES = frozenset(
    (
        *("none", "bool", "int", "float", "decimal", "complex", "str", "bytes"),
        *("date", "time", "datetime", "timedelta", "uuid", "url", "multi-host-url"),
    )
)
# The keys of the child schemas of the core schemas that only make what their children make. Any other schema,
# such as `any`, validators, or custom ones, can make anything.
_PASS_THROUGH_SCHEMA_KEYS = {
    **dict.fromkeys(("list", "set", "frozenset", "generator", "tuple"), ("items_schema",)),
    "dict": ("keys_schema", "values_schema"),
    **dict.fromkeys(
        ("nullable", "default", "custom-error", "model", "model-field", "dataclass", "dataclass-field"), ("schema",)
    ),
    **dict.fromkeys(("union", "tagged-union"), ("choices",)),
    "lax-or-strict": ("lax_schema", "strict_schema"),
    "json-or-python": ("json_schema", "python_schema"),
    "chain": ("steps",),
    **dict.fromkeys(("model-fields", "dataclass-args"), ("fields",)),
}


def _can_schema_make_members_of_plain_enum_copies(schema: dict[str, Any]) -> bool:
    stack = [_register_core_schema_definitions(schema)]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        kind = node["type"]
        if kind in _SCHEMAS_OF_BUILTIN_VALUES:
            continue
        if kind == "literal":
            if any(isinstance(value, Enum) for value in node["expected"]):
                return True
        elif kind == "enum":
            head_enum = getattr(node["cls"], "__cadwyn_original_model__", None)
            if head_enum is not None and not is_enum_looked_up_by_value(head_enum):
                return True
        elif kind == "definition-ref":
            if node["schema_ref"] not in _CORE_SCHEMA_DEFINITIONS:  # pragma: no cover # Pydantic registers them all
                return True
            stack.append(_CORE_SCHEMA_DEFINITIONS[node["schema_ref"]])
        elif (
            kind not in _PASS_THROUGH_SCHEMA_KEYS
            # Custom `__init__` and `__post_init__` run code of users
            or node.get("custom_init")
            or node.get("post_init")
            # Extra values of models and dataclasses are not validated
            or node.get("config", {}).get("extra_fields_behavior") == "allow"
            or node.get("extra_behavior") == "allow"
        ):
            return True
        else:
            for key in _PASS_THROUGH_SCHEMA_KEYS[kind]:
                if key not in node:
                    # Containers without the schemas of their items take anything
                    return True
                child = node[key]
                if key not in ("fields", "choices") and not isinstance(child, list):
                    stack.append(child)
                    continue
                # Fields and tagged union choices are keyed, and union choices can be `(schema, label)` tuples
                children = child.values() if isinstance(child, dict) else child
                stack.extend(item[0] if isinstance(item, tuple) else item for item in children)
    return False


def _replace_with_head_enum_members(data: Any, *, wrap_lazy_iterators: bool = True) -> Any:
    """Replace each member of a copy of a plain enum or flag with the member of its head enum that has the same value.

    Python-mode dumps keep the members of the copies, which the head rejects for plain enums and flags. The head looks
    up the members of the copies of `str`, `int`, and `float` enums by their values anyway, so we leave them, as well
    as the members of other enums and the ones that their head enums have no member with exactly the same value for,
    so that the head rejects them as before.

    We only walk into the containers that pydantic makes when it validates a field, and copy only the ones in which
    something below them changes, with the same types. Copying them again hashes their keys and items, which runs code
    of users, so if a copy fails, we keep the container as it is. Pydantic's own lazy iterators (of `Iterable` and
    `Generator` fields) get wrapped unless told otherwise, so that their items are replaced only as the endpoint
    consumes them. Anything
    else, such as models (a dump has none, but a migration can put some into the body), other iterators, or
    containers of other types, could run code of users or lose what they hold if we iterated or copied it, so we leave
    it as it is. We walk depth-first without recursion, so that
    data as deep as pydantic accepts never hits the recursion limit, and we don't walk into the containers that contain
    themselves again.
    """
    children = _get_children_that_can_be_enum_members(data)
    if children is None:
        return _replace_with_head_enum_member(data, wrap_lazy_iterators=wrap_lazy_iterators)
    # Each frame is [container, iterator over its children, its children with replaced members, whether any changed]
    stack: list[list[Any]] = [[data, iter(children), [], False]]
    containers_on_stack = {id(data)}
    while True:
        frame = stack[-1]
        child = next(frame[1], _NO_CHILD)
        if child is not _NO_CHILD:
            grandchildren = None if id(child) in containers_on_stack else _get_children_that_can_be_enum_members(child)
            if grandchildren is None:
                replaced_child = _replace_with_head_enum_member(child, wrap_lazy_iterators=wrap_lazy_iterators)
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


def _replace_with_head_enum_member(value: Any, *, wrap_lazy_iterators: bool) -> Any:
    if wrap_lazy_iterators and type(value) in _PYDANTIC_ITERATOR_TYPES:
        return map(_replace_with_head_enum_members, value)
    if not isinstance(value, Enum):
        return value
    head_enum: Union[type[Enum], None] = getattr(type(value), "__cadwyn_original_model__", None)
    if head_enum is None or is_enum_looked_up_by_value(head_enum):
        return value
    head_member = get_member_with_same_value(head_enum, value)
    return value if head_member is None else head_member


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
