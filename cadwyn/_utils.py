import sys
from collections.abc import Callable
from enum import Enum, Flag
from inspect import signature
from typing import TYPE_CHECKING, Any, Concatenate, Generic, TypeVar, Union

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


def is_enum_looked_up_by_value(enum_cls: type[Enum]) -> bool:
    """Whether pydantic takes any member of the enum's type that has the value of a member, e.g. a member of a copy.

    Pydantic looks up the members of `str`, `int`, and `float` enums by their values. Plain enums and flags only take
    their own members, and pass anything else to `_missing_`.
    """
    return issubclass(enum_cls, (int, str, float))


def get_member_with_same_value(enum_cls: type[Enum], member: Enum) -> Union[Enum, None]:
    """Return the member of `enum_cls` that has exactly the value of `member`, or None if it has no such member.

    Unlike `enum_cls(member.value)`, this never calls the `_missing_` of `enum_cls`, which can map the value to another
    member, and it never takes a member whose value only equals the value of `member` (e.g. `1` for `True`). For flags,
    it takes the combinations of members that have the value, but not the ones that the boundary of `enum_cls` makes of
    a value with other bits (e.g. `CONFORM` drops the bits that `enum_cls` doesn't have).
    """
    value = member._value_
    try:
        try:
            candidate = enum_cls._value2member_map_.get(value)
        except TypeError:  # Enums keep the members with unhashable values only in their member map
            candidate = next((other for other in enum_cls._member_map_.values() if other._value_ == value), None)
        if candidate is None and issubclass(enum_cls, Flag) and type(value) is int:
            # Flags create the combinations of their members on lookup
            candidate = Flag._missing_.__func__(enum_cls, value)
        is_same_value = (
            type(candidate) is enum_cls and type(candidate._value_) is type(value) and candidate._value_ == value
        )
    except Exception:  # noqa: BLE001 # E.g. a value whose `__eq__` fails, or a flag boundary that rejects the bits
        return None
    return candidate if is_same_value else None
