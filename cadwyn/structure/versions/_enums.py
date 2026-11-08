import functools
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterator
from enum import Enum
from typing import Any, Union, cast

from fastapi._compat import ModelField
from fastapi.routing import APIRoute
from pydantic_core import SchemaSerializer, SchemaValidator, core_schema
from typing_extensions import override

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


def _can_body_have_lazy_iterators(route: APIRoute, *, in_lazy_items: bool) -> bool:
    """Whether the request body of the route can have Pydantic's lazy iterators, or such iterators in their items.

    We only say no when the schema of its single body param has no generator schema (in the items of a generator
    schema), and check it once per route, so that the bodies of other routes are never walked.
    """
    if len(route.dependant.body_params) != 1:
        return True
    field = route.dependant.body_params[0]
    cached = _CAN_BODY_HAVE_LAZY_ITERATORS.get((id(field), in_lazy_items))
    if cached is not None and cached[0] is field:
        return cached[1]
    result = _has_generator_schema(cast("dict[str, Any]", field._type_adapter.core_schema), in_items=in_lazy_items)
    _CAN_BODY_HAVE_LAZY_ITERATORS[id(field), in_lazy_items] = field, result
    return result


_CAN_BODY_HAVE_LAZY_ITERATORS: dict[tuple[int, bool], tuple[ModelField, bool]] = {}


def _has_generator_schema(schema: dict[str, Any], *, in_items: bool = False) -> bool:
    """Whether the core schema has a generator schema anywhere, or in the items of a generator schema.

    Only generator schemas make Pydantic's lazy iterators. Any other value that holds one (e.g. a validator's) has
    been made by code of users, and we leave iterators of users as they are. We look through every value of every
    schema, so that we find the generator schemas in schemas that we don't know too.
    """
    stack: list[Any] = [_register_core_schema_definitions(schema)]
    seen: set[int] = set()
    while stack:
        value = stack.pop()
        if not isinstance(value, dict | list | tuple) or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, dict):
            kind = value.get("type")
            if kind == "generator":
                # A generator schema without the schema of its items takes what the client has sent
                if not in_items or ("items_schema" in value and _has_generator_schema(value["items_schema"])):
                    return True
                continue
            if kind == "definition-ref":
                if value["schema_ref"] not in _CORE_SCHEMA_DEFINITIONS:  # pragma: no cover # Pydantic registers them
                    return True
                stack.append(_CORE_SCHEMA_DEFINITIONS[value["schema_ref"]])
            stack.extend(value.values())
        else:
            stack.extend(value)
    return False


def _to_head_values(
    data: Any,
    *,
    replace_enum_members: bool,
    replay_lazy_iterators: bool,
    walk_lazy_item: Union[Callable[[Any], Any], None],
) -> Any:
    """Return the data with the values that the head needs instead of the ones of the request's version.

    Python-mode dumps keep the members of the copies of enums, which the head rejects for plain enums and flags, so
    we replace each of them with the member of its head enum that has the same value. The head looks up the members
    of the copies of `str`, `int`, and `float` enums by their values anyway, so we leave them, as well as the members
    of other enums and the ones that their head enums have no member with exactly the same value for, so that the head
    rejects them as before.

    Python-mode dumps also keep a single lazy iterator for each value of an `Iterable` or `Generator` field. Every
    param that reads the body validates it on its own, so they would all share that iterator, and each would only get
    the items that the others haven't consumed. In plain FastAPI, each of them gets its own iterator over the JSON
    that the client has sent. So if several params read the body, we give each of Pydantic's own lazy iterators a
    `_ReplayableIterable`, which hands every param all of its items, consuming them only as the params do. If given
    `walk_lazy_item`, the items go through it as the params consume them.

    We only walk into the containers that pydantic makes when it validates a field, and copy only the ones in which
    something below them changes, with the same types. Copying them again hashes their keys and items, which runs code
    of users, so if a copy fails, we keep the container as it is. Anything else, such as models (a dump has none, but a
    migration can put some into the body), other iterators, or containers of other types, could run code of users or
    lose what they hold if we iterated or copied it, so we leave it as it is. We walk depth-first without recursion, so
    that data as deep as pydantic accepts never hits the recursion limit, and we don't walk into the containers that
    contain themselves again.
    """
    to_head_value = functools.partial(
        _to_head_value,
        replace_enum_members=replace_enum_members,
        replay_lazy_iterators=replay_lazy_iterators,
        walk_lazy_item=walk_lazy_item,
    )
    children = _get_children_to_walk(data)
    if children is None:
        return to_head_value(data)
    # Each frame is [container, iterator over its children, its children with head values, whether any changed]
    stack: list[list[Any]] = [[data, iter(children), [], False]]
    containers_on_stack = {id(data)}
    while True:
        frame = stack[-1]
        child = next(frame[1], _NO_CHILD)
        if child is not _NO_CHILD:
            grandchildren = None if id(child) in containers_on_stack else _get_children_to_walk(child)
            if grandchildren is None:
                head_child = to_head_value(child)
                frame[2].append(head_child)
                frame[3] = frame[3] or head_child is not child
            else:
                stack.append([child, iter(grandchildren), [], False])
                containers_on_stack.add(id(child))
            continue
        stack.pop()
        container = frame[0]
        containers_on_stack.discard(id(container))
        head_container = _copy_container_with_children(container, frame[2]) if frame[3] else container
        if not stack:
            return head_container
        stack[-1][2].append(head_container)
        stack[-1][3] = stack[-1][3] or head_container is not container


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


def _get_lazy_item_walker(*, replace_enum_members: bool, replay_lazy_iterators: bool) -> Callable[[Any], Any]:
    """Return what gives the items of lazy iterators, and the items of lazy iterators in them, the head's values"""

    def walk_lazy_item(item: Any) -> Any:
        return _to_head_values(
            item,
            replace_enum_members=replace_enum_members,
            replay_lazy_iterators=replay_lazy_iterators,
            walk_lazy_item=walk_lazy_item,
        )

    return walk_lazy_item


def _to_head_value(
    value: Any,
    *,
    replace_enum_members: bool,
    replay_lazy_iterators: bool,
    walk_lazy_item: Union[Callable[[Any], Any], None],
) -> Any:
    if type(value) in _PYDANTIC_ITERATOR_TYPES:
        if walk_lazy_item is not None:
            value = map(walk_lazy_item, value)
        return _ReplayableIterable(value) if replay_lazy_iterators else value
    if not replace_enum_members or not isinstance(value, Enum):
        return value
    head_enum: Union[type[Enum], None] = getattr(type(value), "__cadwyn_original_model__", None)
    if head_enum is None or is_enum_looked_up_by_value(head_enum):
        return value
    head_member = get_member_with_same_value(head_enum, value)
    return value if head_member is None else head_member


def _get_children_to_walk(value: Any) -> Union[list[Any], None]:
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


class _ReplayableIterable:
    """An iterable whose every iterator yields all items of a single iterator from the start.

    It takes each item from the iterator only when one of its iterators needs it, and keeps it for the others. Like
    Pydantic's lazy iterators, its iterators raise each error that the iterator raises instead of an item, and can go
    on to the next items after it.
    """

    __slots__ = ("_iterator", "_outcomes")

    def __init__(self, iterator: Iterator[Any]) -> None:
        self._iterator: Union[Iterator[Any], None] = iterator
        # Each item of the iterator, or the error that it raised instead, in order
        self._outcomes: list[tuple[Any, Union[Exception, None]]] = []

    def __iter__(self) -> "_ReplayIterator":
        return _ReplayIterator(self)

    def _get_outcome(self, index: int) -> tuple[Any, Union[Exception, None]]:
        if index == len(self._outcomes):
            if self._iterator is None:
                raise StopIteration
            try:
                self._outcomes.append((next(self._iterator), None))
            except StopIteration:
                self._iterator = None
                raise
            except Exception as error:  # noqa: BLE001 # Each iterator raises it when it gets to it
                self._outcomes.append((None, error))
        return self._outcomes[index]


class _ReplayIterator(Iterator[Any]):
    __slots__ = ("_index", "_iterable")

    def __init__(self, iterable: _ReplayableIterable) -> None:
        self._iterable = iterable
        self._index = 0

    @override
    def __next__(self) -> Any:
        item, error = self._iterable._get_outcome(self._index)
        self._index += 1
        if error is not None:
            raise error
        return item
