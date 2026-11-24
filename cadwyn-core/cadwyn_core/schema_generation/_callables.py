import copy
import functools
import inspect
import sys
import types
import typing
from collections.abc import Callable
from enum import Enum, auto
from typing import (
    TYPE_CHECKING,
    Any,
    TypeGuard,
    Union,
)

from typing_extensions import (
    override,
)

from cadwyn_core._utils import iscoroutinefunction
from cadwyn_core.structure.versions._common import (
    _CADWYN_REQUEST_PARAM_NAME,
    _CADWYN_RESPONSE_PARAM_NAME,
)

if TYPE_CHECKING:
    from fastapi.routing import APIRoute
else:
    from cadwyn_core._stand_ins import APIRoute  # noqa: TC001


def is_regular_function(call: object) -> TypeGuard[Union[types.FunctionType, types.MethodType]]:
    return isinstance(call, types.FunctionType | types.MethodType)


def _function_globals(call: Union[types.FunctionType, types.MethodType]) -> dict[str, Any]:
    return call.__globals__  # ty: ignore[missing-union-attribute]


def _get_globals(call: Any) -> dict[str, Any]:
    if is_regular_function(call):
        return _function_globals(call)
    dunder_call = inspect.getattr_static(call, "__call__", None)
    return _function_globals(dunder_call) if is_regular_function(dunder_call) else {}


def _unwrap_partial(call: Any) -> Any:
    while isinstance(call, functools.partial):
        call = call.func
    return call


def _get_function_behind_callable(call: Any) -> Any:
    """Return the function that runs when the callable is called. It decides how FastAPI must call the callable.

    Partials only pre-fill arguments so we unwrap them. We do not follow decorators (__wrapped__) because the outer
    decorator is what runs. Classes are returned as is because calling them constructs an instance.
    """
    call = _unwrap_partial(call)
    if is_regular_function(call) or inspect.isclass(call):
        return call
    return call.__call__


def _get_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its postponed (string) annotations evaluated, like FastAPI does.

    inspect.signature() itself evaluates them in the globals of whichever function it reads the signature from,
    which is the only reliable way to pick the right globals for every kind of wrapper (partials, decorators, etc).
    It evaluates them only once so the forward references within them (e.g. `body: "Model"` or `list["Model"]`
    with postponed annotations) are left.
    """
    try:
        return inspect.signature(call, eval_str=True)
    except NameError:
        return _get_unevaluated_signature(call)


def _get_unevaluated_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its annotations as written, e.g. strings for postponed ones"""
    if sys.version_info >= (3, 14):
        from annotationlib import Format

        return inspect.signature(call, annotation_format=Format.FORWARDREF)
    return inspect.signature(call)


_PARTIAL_DICT_ACCESSOR = vars(functools.partial)["__dict__"]


def _get_best_effort_globals(call: Any) -> dict[str, Any]:
    """Return the globals of the callable for the string annotations that _get_signature() left.

    Like FastAPI, we take them from the unwrapped callable. Unlike FastAPI, we also unwrap partials that do not
    carry globals themselves. For callable class instances, __globals__ is on the __call__ method instead.
    Partial subclasses that override __call__ are treated like other callable instances, just like Cadwyn always did.
    """
    unwrapped_call = inspect.unwrap(call)
    while True:
        is_partial_with_overridden_call = isinstance(unwrapped_call, functools.partial) and (
            type(unwrapped_call).__call__ is not functools.partial.__call__
        )
        if is_partial_with_overridden_call and issubclass(type(unwrapped_call), functools.partial):
            _PARTIAL_DICT_ACCESSOR.__get__(unwrapped_call)
        unwrapped_call_globals = getattr(unwrapped_call, "__globals__", None)
        if isinstance(unwrapped_call_globals, dict):
            return unwrapped_call_globals
        if not isinstance(unwrapped_call, functools.partial) or is_partial_with_overridden_call:
            break
        unwrapped_call = inspect.unwrap(unwrapped_call.func)
    dunder_call = inspect.getattr_static(unwrapped_call, "__call__", None)
    return _function_globals(dunder_call) if is_regular_function(dunder_call) else {}


class _CallableKind(Enum):
    ENDPOINT = auto()
    DEPENDENCY = auto()
    OTHER = auto()


class _CallableWrapper:
    __annotations__: dict[str, Any]
    __defaults__: Union[tuple[Any, ...], None]
    __signature__: inspect.Signature
    _is_called_by_fastapi = False

    def __init__(self, original_callable: Callable[..., Any], *, is_called_by_fastapi: bool) -> None:
        super().__init__()
        self._original_callable = original_callable
        if is_called_by_fastapi:
            self._is_called_by_fastapi = True
            wrapped = _unwrap_partial(original_callable)
            functools.update_wrapper(
                self, wrapped, updated=functools.WRAPPER_UPDATES if is_regular_function(wrapped) else ()
            )
        else:
            if not is_regular_function(original_callable):
                original_callable = original_callable.__call__

            functools.update_wrapper(self, original_callable)

    @property
    def __globals__(self) -> dict[str, Any]:  # pragma: no cover
        if is_regular_function(self._original_callable):
            return _function_globals(self._original_callable)
        dunder_call = self._original_callable.__call__
        if is_regular_function(dunder_call):
            return _function_globals(dunder_call)
        return {}

    def __deepcopy__(self, memo: dict[int, Any]) -> "_CallableWrapper":
        if not self._is_called_by_fastapi:
            wrapper_copy = object.__new__(type(self))
            memo[id(self)] = wrapper_copy
            wrapper_copy.__dict__.update(copy.deepcopy(self.__dict__, memo))
            return wrapper_copy
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._original_callable(*args, **kwargs)

    @override
    def __hash__(self) -> int:
        return hash(self._original_callable)

    @override
    def __eq__(self, value: object) -> bool:
        return self._original_callable == value


class _AsyncCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._original_callable(*args, **kwargs)


class _GeneratorCallableWrapper(_CallableWrapper):
    @override
    def __call__(self, *args: Any, **kwargs: Any) -> typing.Iterator[Any]:
        yield from self._original_callable(*args, **kwargs)


class _AsyncGeneratorCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> typing.AsyncGenerator[Any, Any]:
        generator = self._original_callable(*args, **kwargs)
        try:
            value = await generator.__anext__()
            while True:
                sent_value = thrown_exception = None
                try:
                    sent_value = yield value
                except BaseException as e:  # noqa: BLE001
                    thrown_exception = e
                if thrown_exception is None:
                    value = await generator.asend(sent_value)
                else:
                    value = await generator.athrow(thrown_exception)
        except StopAsyncIteration:
            return


def is_gen_callable(call: Callable[..., object]) -> bool:
    if inspect.isgeneratorfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isgeneratorfunction(dunder_call)


def is_async_gen_callable(call: Callable[..., object]) -> bool:
    if inspect.isasyncgenfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isasyncgenfunction(dunder_call)


def is_coroutine_callable(call: Callable[..., object]) -> bool:  # pragma: no cover
    if inspect.isroutine(call):
        return iscoroutinefunction(call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]
    if inspect.isclass(call):
        return False
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return iscoroutinefunction(dunder_call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]


def _add_request_and_response_params(route: "APIRoute"):
    if not route.dependant.request_param_name:
        route.dependant.request_param_name = _CADWYN_REQUEST_PARAM_NAME
    if not route.dependant.response_param_name:
        route.dependant.response_param_name = _CADWYN_RESPONSE_PARAM_NAME
