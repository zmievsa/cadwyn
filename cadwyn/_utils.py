import dataclasses
import math
import sys
from collections.abc import Callable
from decimal import Decimal
from inspect import signature
from typing import TYPE_CHECKING, Any, Concatenate, Generic, TypeVar, Union, get_args, get_origin

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


def is_same_value(left: Any, right: Any) -> bool:
    """Whether Cadwyn can be sure that the values are the same, so changing one to the other changes nothing.

    `==` is not enough: `True == 1`, `1.0 == 1`, `-0.0 == 0.0`, `Decimal("1.0") == Decimal("1")`, and
    `Union[int, str] == Union[str, int]`, while OpenAPI and serialization tell them apart. So we compare only what we
    can fully inspect cheaply: `str`, `bytes`, `int`, `float`, `complex`, and `Decimal` values of the same type,
    `list`, `tuple`, `dict`, `set`, and `frozenset` (but not their subclasses) with the same items, dataclass instances
    that have no state besides their fields, and type hints. Anything else, including a value that refers to itself,
    is the same only if it is the same object. When we can't be sure, the values are different, so we never reject a
    real change.
    """
    return _is_same_value(left, right, set())


def _is_same_value(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    pair = (id(left), id(right))
    if pair in pairs_being_compared:
        # The values refer to themselves, e.g. a list that contains itself
        return False
    pairs_being_compared.add(pair)
    try:
        return _have_same_contents(left, right, pairs_being_compared)
    finally:
        pairs_being_compared.discard(pair)


def _have_same_contents(left: Any, right: Any, pairs_being_compared: set[tuple[int, int]]) -> bool:
    """Compare two values of the same type without calling `__eq__` of objects that can compare in any way"""
    value_type = type(left)
    if value_type in (str, bytes, int):
        return left == right
    if value_type is float:
        # -0.0 == 0.0. NaN is never equal to another NaN
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    if value_type is complex:
        return _is_same_value(left.real, right.real, pairs_being_compared) and _is_same_value(
            left.imag, right.imag, pairs_being_compared
        )
    if value_type is Decimal:
        # Decimal("1.0") == Decimal("1") and a signaling NaN raises in ==
        return not left.is_nan() and left.as_tuple() == right.as_tuple()
    if value_type in (list, tuple):
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
    if value_type in (set, frozenset):
        if len(left) != len(right) or left != right:
            return False
        # The sets are equal, so each item has an equal item (e.g. 1 for True) in the other set
        right_items = {item: item for item in right}
        return all(_is_same_value(item, right_items.get(item, Sentinel), pairs_being_compared) for item in left)
    if dataclasses.is_dataclass(left) and not isinstance(left, type):
        # E.g. AliasChoices, FastAPI's Depends, and constraints from annotated_types such as Gt.
        # The dataclass can still tell them apart in its __eq__, e.g. with eq=False
        return (
            _has_only_dataclass_fields(left)
            and _has_only_dataclass_fields(right)
            and all(
                _is_same_value(
                    getattr(left, field.name, Sentinel), getattr(right, field.name, Sentinel), pairs_being_compared
                )
                for field in dataclasses.fields(left)
            )
            and (left == right) is True
        )
    # Type hints such as list[int], Union[int, str], Literal[1, 2], and Annotated[int, Gt(1)]
    origin = get_origin(left)
    return (
        origin is not None
        and _is_same_value(origin, get_origin(right), pairs_being_compared)
        and _is_same_value(get_args(left), get_args(right), pairs_being_compared)
        and (left == right) is True
    )


def _has_only_dataclass_fields(value: Any) -> bool:
    """Whether the dataclass instance has no state that its fields don't show, e.g. a slot or an attribute"""
    field_names = {field.name for field in dataclasses.fields(value)}
    for cls in type(value).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        if any(slot not in field_names and slot not in {"__dict__", "__weakref__"} for slot in slots):
            return False
    return getattr(value, "__dict__", {}).keys() <= field_names
