import copy
import functools
import inspect
import sys
import types
import typing
from collections.abc import Callable
from enum import Enum, auto
from typing import (
    Any,
    TypeGuard,
    Union,
)

from fastapi.routing import APIRoute
from typing_extensions import (
    override,
)

from cadwyn._utils import iscoroutinefunction
from cadwyn.structure.versions import (
    _CADWYN_REQUEST_PARAM_NAME,
    _CADWYN_RESPONSE_PARAM_NAME,
)


def is_regular_function(call: object) -> TypeGuard[Union[types.FunctionType, types.MethodType]]:
    return isinstance(call, types.FunctionType | types.MethodType)


def _function_globals(call: Union[types.FunctionType, types.MethodType]) -> dict[str, Any]:
    return call.__globals__  # ty: ignore[unresolved-attribute]  # MethodType forwards function attributes at runtime.


def _get_globals(call: Any) -> dict[str, Any]:
    # For callable class instances, __globals__ is on the __call__ method, not on the instance itself
    if is_regular_function(call):
        return _function_globals(call)
    dunder_call = inspect.getattr_static(call, "__call__", None)
    return _function_globals(dunder_call) if is_regular_function(dunder_call) else {}


def _unwrap_partial(call: Any) -> Any:
    # The same as FastAPI's _impartial(), which decides how FastAPI calls the original dependency
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
    # This is deliberately narrow: an annotation may use a name that only exists for type checkers
    # (e.g. one imported under "if TYPE_CHECKING:"). FastAPI handles this the same way. The parameter annotations
    # that can be resolved individually are resolved later in _modify_callable_annotations
    except NameError:
        return _get_unevaluated_signature(call)


def _get_unevaluated_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its annotations as written, e.g. strings for postponed ones"""
    if sys.version_info >= (3, 14):
        from annotationlib import Format

        # Unresolvable names in lazily evaluated annotations become ForwardRefs instead of raising
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
        # Proxies can pass the isinstance() check by faking __class__ but they have no partial __dict__ to create
        if is_partial_with_overridden_call and issubclass(type(unwrapped_call), functools.partial):
            # This has nothing to do with globals but it must stay: accessing the instance __dict__ of a partial
            # creates it. Python 3.13+ flattens a partial of a partial (e.g. the one Starlette's run_in_threadpool
            # makes) into a single partial of the inner .func if the inner partial has no __dict__, which would
            # silently skip the overridden __call__ of a partial subclass. Cadwyn used to create the __dict__ as
            # a side effect of inspect.getattr_static(partial, "__call__") so we keep doing it explicitly.
            # We use the accessor of partial itself so that __getattribute__ or __dict__ of subclasses cannot
            # interfere, and we do it before anything else so that it also happens for the subclasses that
            # forward __globals__ to their .func (e.g. through __getattr__)
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
    # Callables that FastAPI never calls itself, e.g. pydantic's default factories or callables in Annotated metadata
    OTHER = auto()


class _CallableWrapper:
    __annotations__: dict[str, Any]
    __defaults__: Union[tuple[Any, ...], None]
    __signature__: inspect.Signature
    # Whether it wraps an endpoint or a dependency rather than a callable that FastAPI never calls itself
    # (e.g. pydantic's default factories). The latter are wrapped exactly the way Cadwyn always did
    _is_called_by_fastapi = False

    # __eq__ and __hash__ are needed to make sure that dependency overrides work correctly.
    # They are based on putting dependencies (functions) as keys for the dictionary so if we want to be able to
    # override the wrapper, we need to make sure that it is equivalent to the original in __hash__ and __eq__

    def __init__(self, original_callable: Callable[..., Any], *, is_called_by_fastapi: bool) -> None:
        super().__init__()
        self._original_callable = original_callable
        if is_called_by_fastapi:
            self._is_called_by_fastapi = True
            # FastAPI follows __wrapped__ to find out how to call a dependency so it must lead to the same place
            # as it would for the original. We only copy the attributes of functions because copying the ones of
            # classes and their instances would make us look like them to FastAPI
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
        # FastAPI uses __globals__ to resolve forward references in type hints
        # It's supposed to be an attribute on the function but we use it as property to prevent python
        # from trying to pickle globals when we deepcopy this wrapper
        if is_regular_function(self._original_callable):
            return _function_globals(self._original_callable)
        dunder_call = self._original_callable.__call__
        if is_regular_function(dunder_call):
            return _function_globals(dunder_call)
        return {}

    def __deepcopy__(self, memo: dict[int, Any]) -> "_CallableWrapper":
        if not self._is_called_by_fastapi:
            # The same as the default deepcopy, which is what Cadwyn always did for the other callables
            wrapper_copy = object.__new__(type(self))
            memo[id(self)] = wrapper_copy
            wrapper_copy.__dict__.update(copy.deepcopy(self.__dict__, memo))
            return wrapper_copy
        # Copying would also copy the original dependency (e.g. a class instance or a partial) so it would stop
        # matching the keys of app.dependency_overrides and changes to its state would not be visible in the copy.
        # Wrappers are never mutated after creation and every version creates its own wrappers anyway.
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
        # "yield from" forwards send(), throw(), and close() so the wrapped generator handles exceptions itself
        yield from self._original_callable(*args, **kwargs)


class _AsyncGeneratorCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> typing.AsyncGenerator[Any, Any]:
        # There is no "yield from" for async generators so we forward asend() and athrow() by hand.
        # Otherwise, exceptions (e.g. the ones raised in the endpoint) would never reach the wrapped generator
        # so its "except" and "finally" blocks would get skipped until it gets garbage collected.
        # aclose() is forwarded too because it throws GeneratorExit into us.
        # The exception is forwarded outside of the "except" block. Otherwise, it would still be the one being handled
        # while the wrapped generator runs which would change sys.exc_info() and the __context__ of its exceptions.
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
    # Copied from fastapi.dependencies.models
    if inspect.isgeneratorfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isgeneratorfunction(dunder_call)


def is_async_gen_callable(call: Callable[..., object]) -> bool:
    # Copied from fastapi.dependencies.models
    if inspect.isasyncgenfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isasyncgenfunction(dunder_call)


def is_coroutine_callable(call: Callable[..., object]) -> bool:  # pragma: no cover
    # Copied from fastapi.dependencies.models
    # ty reports the deprecation only on Python 3.11/3.12. Repeating unused-ignore-comment
    # handles the other targets: https://github.com/astral-sh/ty/issues/3393.
    if inspect.isroutine(call):
        return iscoroutinefunction(call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]  # Match FastAPI.
    if inspect.isclass(call):
        return False
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return iscoroutinefunction(dunder_call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]  # Match FastAPI.


def _add_request_and_response_params(route: APIRoute):
    if not route.dependant.request_param_name:
        route.dependant.request_param_name = _CADWYN_REQUEST_PARAM_NAME
    if not route.dependant.response_param_name:
        route.dependant.response_param_name = _CADWYN_RESPONSE_PARAM_NAME
