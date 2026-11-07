import dataclasses
import math
import sys
from collections.abc import Callable
from decimal import Decimal
from inspect import signature
from typing import TYPE_CHECKING, Any, Concatenate, Generic, TypeVar, Union, get_args, get_origin

import annotated_types
import fastapi.params
import pydantic
from pydantic._internal._decorators import DecoratedType, unwrap_wrapped_function
from typing_extensions import ParamSpec, override

if TYPE_CHECKING:
    from typing_extensions import TypeIs

Sentinel: Any = object()

_P = ParamSpec("_P")
_R = TypeVar("_R")
_SourceSelf = TypeVar("_SourceSelf")


_P_T = TypeVar("_P_T")
_P_R = TypeVar("_P_R")

# Match FastAPI's version-dependent coroutine detection. Before Python 3.13,
# asyncio also recognizes the legacy _is_coroutine marker. Using inspect instead
# would make Cadwyn treat some dependencies as synchronous while FastAPI awaits them.
# Keep this split aligned with FastAPI, even while ty reports asyncio's API as deprecated.
if sys.version_info >= (3, 13):  # pragma: no cover
    from inspect import iscoroutinefunction
else:  # pragma: no cover
    from asyncio import iscoroutinefunction  # noqa: F401


UnionType = type(int | str) | type(Union[int, str])
DATACLASS_SLOTS: dict[str, Any] = {"slots": True}
DATACLASS_KW_ONLY: dict[str, Any] = {"kw_only": True}


def get_name_of_function_wrapped_in_pydantic_validator(func: Any) -> str:
    if hasattr(func, "wrapped"):
        return get_name_of_function_wrapped_in_pydantic_validator(func.wrapped)
    if hasattr(func, "__func__"):
        return get_name_of_function_wrapped_in_pydantic_validator(func.__func__)
    return func.__name__


class classproperty(Generic[_P_T, _P_R]):  # noqa: N801
    def __init__(self, func: Callable[[_P_T], _P_R]) -> None:
        super().__init__()
        self.func = func

    def __get__(self, obj: Any, cls: _P_T) -> _P_R:
        return self.func(cls)


class PlainRepr(str):
    """String class where repr doesn't include quotes"""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return str(self)


def same_method_definition_as_in(
    t: Callable[Concatenate[_SourceSelf, _P], _R],
) -> Callable[[Callable[..., _R]], Callable[Concatenate[object, _P], _R]]:
    def decorator(f: Callable[..., _R]) -> Callable[Concatenate[object, _P], _R]:
        f.__signature__ = signature(t)  # ty: ignore[unresolved-attribute]
        return f

    return decorator


def fully_unwrap_decorator(
    func: "DecoratedType[object]",
    is_pydantic_v1_style_validator: Any,
) -> Callable[..., object]:
    func = unwrap_wrapped_function(func)
    if is_pydantic_v1_style_validator and func.__closure__:
        func = func.__closure__[0].cell_contents
    return unwrap_wrapped_function(func)


_SubclassT = TypeVar("_SubclassT")


# Unlike issubclass, accepts non-class annotations (unions, generic aliases, etc.) and returns False for them
def lenient_issubclass(
    cls: object, other: Union[type[_SubclassT], tuple[type[_SubclassT], ...]]
) -> "TypeIs[type[_SubclassT]]":
    try:
        # Non-class annotations raise TypeError, which is handled below
        return issubclass(cls, other)  # ty: ignore[invalid-argument-type]
    except TypeError:  # pragma: no cover
        return False


def _callable_name(call: Callable[..., object]) -> str:
    name = getattr(call, "__name__", None)
    return name if isinstance(name, str) else type(call).__name__


def is_sqlalchemy_model(model: type) -> bool:
    # __mapper__ covers imperative mappings; _sa_registry also covers declarative
    # bases and SQLModel classes without table=True. Avoid importing optional dependencies.
    return hasattr(model, "__mapper__") or hasattr(model, "_sa_registry")


# We look types up by their ids because hashing a type can run the __hash__ of the user's metaclass
_SCALAR_TYPE_IDS = frozenset(map(id, (type(None), bool, int, float, complex, str, bytes, Decimal)))
# Dataclasses of the libraries that Cadwyn supports, whose fields are all of their state, even though some of them have
# bases that aren't dataclasses
_ALLOWED_DATACLASS_IDS = frozenset(
    map(
        id,
        (
            pydantic.AliasChoices,
            pydantic.AliasPath,
            pydantic.Discriminator,
            pydantic.StringConstraints,
            pydantic.Strict,
            pydantic.AllowInfNan,
            pydantic.FailFast,
            fastapi.params.Depends,
            fastapi.params.Security,
            annotated_types.Gt,
            annotated_types.Ge,
            annotated_types.Lt,
            annotated_types.Le,
            annotated_types.MultipleOf,
            annotated_types.MinLen,
            annotated_types.MaxLen,
            annotated_types.Len,
            annotated_types.Interval,
            annotated_types.Timezone,
            annotated_types.Predicate,
        ),
    )
)
_TYPE_HINT_MODULES = frozenset({"typing", "types", "typing_extensions"})


def is_same_value(left: Any, right: Any) -> bool:
    """Whether Cadwyn can be sure that the values are the same, so changing one to the other changes nothing.

    `==` is not enough: `True == 1`, `1.0 == 1`, `-0.0 == 0.0`, `Decimal("1.0") == Decimal("1")`, and
    `Union[int, str] == Union[str, int]`, while OpenAPI and serialization tell them apart. Different objects are the
    same only if both have exactly the same type and they are:

    * `None`, `bool`, `int`, `float`, `complex`, `str`, `bytes`, or `Decimal` values with the same value, sign, and
      exponent (NaN is never the same as another NaN object)
    * `list`, `tuple`, or `dict` values whose items (and keys) are the same in the same order, or `set` or `frozenset`
      values whose items are the same and are all such builtin values, so that hashing and comparing them never runs
      the user's code
    * instances of dataclasses whose bases are only `object` and dataclasses, that have no state besides their fields
      and don't opt out of `__eq__`, or of the dataclasses of pydantic, FastAPI, and annotated_types in
      `_ALLOWED_DATACLASS_IDS`, whose fields are the same
    * type hints from `typing` and `types` whose origins and args are the same

    Anything else, including a value that refers to itself, is the same only if it is the same object. We never call
    `__eq__` or `__hash__` of other objects because they can do anything (e.g. numpy arrays compare element-wise).
    When we can't be sure, the values are different, so we never reject a real change.
    """
    try:
        return _is_same_value(left, right, set())
    except Exception:  # noqa: BLE001
        # E.g. reading a field of a dataclass runs the user's property that raises
        return False


def _is_same_value(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if id(type(left)) in _SCALAR_TYPE_IDS:
        return _are_same_scalars(left, right)
    pair = (id(left), id(right))
    if pair in pairs_being_compared:
        # The values refer to themselves, e.g. a dataclass whose field is the dataclass itself
        return False
    pairs_being_compared.add(pair)
    try:
        return _have_same_contents(left, right, pairs_being_compared)
    finally:
        pairs_being_compared.discard(pair)


def _are_same_scalars(left: Any, right: Any) -> bool:
    value_type = type(left)
    if value_type is float:
        # -0.0 == 0.0. NaN is never equal to another NaN
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if value_type is complex:
        return _are_same_scalars(left.real, right.real) and _are_same_scalars(left.imag, right.imag)
    if value_type is Decimal:
        # Decimal("1.0") == Decimal("1") and a signaling NaN raises in ==
        return not left.is_nan() and left.as_tuple() == right.as_tuple()
    return left == right


def _have_same_contents(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    value_type = type(left)
    if value_type is list or value_type is tuple:
        return len(left) == len(right) and all(
            _is_same_value(left_item, right_item, pairs_being_compared)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if value_type is dict:
        # The order of the keys matters because it is the order of the keys in JSON
        return len(left) == len(right) and all(
            _is_same_value(left_key, right_key, pairs_being_compared)
            and _is_same_value(left_value, right_value, pairs_being_compared)
            for (left_key, left_value), (right_key, right_value) in zip(left.items(), right.items(), strict=True)
        )
    if value_type is set or value_type is frozenset:
        if not all(map(_is_builtin_hashable, left)) or not all(map(_is_builtin_hashable, right)):
            return False
        if len(left) != len(right) or left != right:
            return False
        # The sets are equal, so each item has an equal item (e.g. 1 for True) in the other set
        right_items = {item: item for item in right}
        return all(_is_same_value(item, right_items[item], pairs_being_compared) for item in left)
    field_names = _get_names_of_dataclass_fields_that_are_all_of_its_state(value_type)
    if field_names is not None:
        return (
            _has_no_attributes_besides(left, field_names)
            and _has_no_attributes_besides(right, field_names)
            and all(
                _is_same_value(getattr(left, name), getattr(right, name), pairs_being_compared) for name in field_names
            )
        )
    # Type hints such as list[int], Union[int, str], Literal[1, 2], and Annotated[int, Gt(1)]
    origin = get_origin(left) if value_type.__module__ in _TYPE_HINT_MODULES else None
    return (
        origin is not None
        and _is_same_value(origin, get_origin(right), pairs_being_compared)
        and _is_same_value(get_args(left), get_args(right), pairs_being_compared)
    )


def _is_builtin_hashable(value: Any) -> bool:
    """Whether hashing and comparing the value only runs Python's own code"""
    value_type = type(value)
    return id(value_type) in _SCALAR_TYPE_IDS or (
        (value_type is tuple or value_type is frozenset) and all(map(_is_builtin_hashable, value))
    )


def _get_names_of_dataclass_fields_that_are_all_of_its_state(cls: type) -> Union[tuple[str, ...], None]:
    if not dataclasses.is_dataclass(cls):
        return None
    if id(cls) in _ALLOWED_DATACLASS_IDS:
        return tuple(field.name for field in dataclasses.fields(cls))
    # A base that isn't a dataclass, e.g. set, can have state that the fields don't show
    if not all(base is object or "__dataclass_fields__" in base.__dict__ for base in cls.__mro__):
        return None
    # With eq=False, two instances are never equal, so we can't be sure that they are the same
    if cls.__eq__ is object.__eq__:
        return None
    field_names = tuple(field.name for field in dataclasses.fields(cls))
    for base in cls.__mro__:
        slots = base.__dict__.get("__slots__", ())
        slots = (slots,) if isinstance(slots, str) else slots
        if any(slot not in field_names and slot not in {"__dict__", "__weakref__"} for slot in slots):
            return None
    return field_names


def _has_no_attributes_besides(value: Any, names: tuple[str, ...]) -> bool:
    return getattr(value, "__dict__", {}).keys() <= set(names)
