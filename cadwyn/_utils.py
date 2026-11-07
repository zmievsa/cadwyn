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
    `Union[int, str] == Union[str, int]`, while OpenAPI and serialization tell them apart. So the values must have the
    same type, and containers must have the same items in the same order. Other objects (e.g. with a custom `__eq__`)
    are the same only if they are the same object. When we can't be sure, the values are different, so we never
    reject a real change. Different objects are the same only if they are also equal (`==`).
    """
    if left is right:
        return True
    if type(left) is not type(right) or not _have_same_contents(left, right):
        return False
    # A user's __eq__ (e.g. of a dataclass or of a tuple subclass) can still tell them apart. We call it only after
    # comparing the contents, which never calls __eq__ of unknown objects because they can compare in any way
    # (e.g. numpy arrays compare element-wise)
    return (left == right) is True


def _have_same_contents(left: Any, right: Any) -> bool:
    """Compare two values of the same type without calling `__eq__` of unknown objects"""
    value_type = type(left)
    if value_type in (str, bytes, int):
        return True
    if value_type is float:
        # -0.0 == 0.0. NaN is never equal to another NaN
        return math.copysign(1.0, left) == math.copysign(1.0, right)
    if value_type is complex:
        return is_same_value(left.real, right.real) and is_same_value(left.imag, right.imag)
    if value_type is Decimal:
        # Decimal("1.0") == Decimal("1") and a signaling NaN raises in ==
        return not left.is_nan() and left.as_tuple() == right.as_tuple()
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(map(is_same_value, left, right))
    if isinstance(left, dict):
        return len(left) == len(right) and all(
            is_same_value(left_key, right_key) and is_same_value(left_value, right_value)
            for (left_key, left_value), (right_key, right_value) in zip(left.items(), right.items(), strict=True)
        )
    if isinstance(left, (set, frozenset)):
        return len(left) == len(right) and all(any(is_same_value(item, other) for other in right) for item in left)
    if dataclasses.is_dataclass(left) and not isinstance(left, type):
        # E.g. AliasChoices, FastAPI's Depends, and constraints from annotated_types such as Gt
        return all(
            is_same_value(getattr(left, field.name, Sentinel), getattr(right, field.name, Sentinel))
            for field in dataclasses.fields(left)
        )
    # Type hints such as list[int], Union[int, str], Literal[1, 2], and Annotated[int, Gt(1)]
    origin = get_origin(left)
    return (
        origin is not None
        and is_same_value(origin, get_origin(right))
        and is_same_value(get_args(left), get_args(right))
    )
