import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar, Union

from pydantic._internal._decorators import unwrap_wrapped_function

Sentinel: Any = object()

_T = TypeVar("_T", bound=Callable)


_P_T = TypeVar("_P_T")
_P_R = TypeVar("_P_R")

if sys.version_info >= (3, 13):  # pragma: no cover
    from inspect import iscoroutinefunction
else:  # pragma: no cover
    from asyncio import iscoroutinefunction  # noqa: F401


if sys.version_info >= (3, 10):
    UnionType = type(int | str) | type(Union[int, str])
    DATACLASS_SLOTS: dict[str, Any] = {"slots": True}
    ZIP_STRICT_TRUE: dict[str, Any] = {"strict": True}
    ZIP_STRICT_FALSE: dict[str, Any] = {"strict": False}
    DATACLASS_KW_ONLY: dict[str, Any] = {"kw_only": True}
else:
    UnionType = type(Union[int, str])
    DATACLASS_SLOTS: dict[str, Any] = {}
    DATACLASS_KW_ONLY: dict[str, Any] = {}
    ZIP_STRICT_TRUE: dict[str, Any] = {}
    ZIP_STRICT_FALSE: dict[str, Any] = {}


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

    def __repr__(self) -> str:
        return str(self)


def same_definition_as_in(t: _T) -> Callable[[Callable], _T]:
    def decorator(f: Callable) -> _T:
        return f  # pyright: ignore[reportReturnType]

    return decorator


def fully_unwrap_decorator(func: Callable, is_pydantic_v1_style_validator: Any):
    func = unwrap_wrapped_function(func)
    if is_pydantic_v1_style_validator and func.__closure__:
        func = func.__closure__[0].cell_contents
    return unwrap_wrapped_function(func)


T = TypeVar("T", bound=type[object])

if TYPE_CHECKING:
    lenient_issubclass = issubclass

else:

    def lenient_issubclass(cls: type, other: Union[T, tuple[T, ...]]) -> bool:
        try:
            return issubclass(cls, other)
        except TypeError:  # pragma: no cover
            return False
