import dataclasses
import inspect
import threading
import warnings
from collections.abc import (
    Callable,
)
from contextlib import AsyncExitStack
from typing import (
    Union,
)

from fastapi import Request as FastapiRequest
from fastapi.concurrency import run_in_threadpool

from ._common import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME, _logger

_CADWYN_BACKGROUND_TASKS_PARAM_NAME = "cadwyn_background_tasks_param"
_RUN_BACKGROUND_TASKS_FLAG = "run_background_tasks_of_latest_version_dependencies"


def _get_hidden_background_tasks_param_name(endpoint: Callable[..., object]) -> str:
    """Return a name for the BackgroundTasks param that Cadwyn adds to the endpoint, which none of its params has."""
    taken_names = {*inspect.signature(endpoint).parameters, _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME}
    name = _CADWYN_BACKGROUND_TASKS_PARAM_NAME
    while name in taken_names:
        name += "_"
    return name


def _add_keyword_only_parameter(
    func: Callable[..., object],
    param_name: str,
    param_annotation: type,
):
    signature = inspect.signature(func)
    func.__signature__ = signature.replace(  # ty: ignore[missing-attribute]
        parameters=[
            *list(signature.parameters.values()),
            inspect.Parameter(param_name, kind=inspect._ParameterKind.KEYWORD_ONLY, annotation=param_annotation),
        ]
    )


@dataclasses.dataclass
class _DroppedBackgroundTasksWarning:
    """Warns once per route that Cadwyn has dropped the background tasks of the latest version's dependencies."""

    route: str
    has_warned: bool = False
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    async def warn_after_the_response(self, request: FastapiRequest) -> None:
        """Warn once FastAPI has sent the response and run its background tasks, unless the request has failed.

        That's when FastAPI would have run the dropped tasks. It closes this exit stack right after.
        """
        if self.has_warned:
            return
        request_stack = request.scope.get("fastapi_inner_astack")
        if not isinstance(request_stack, AsyncExitStack):
            # Every supported FastAPI version sets it. Without it, we warn right away rather than not at all
            await run_in_threadpool(self._warn, is_deferred=False)
            return

        async def warn_unless_the_request_has_failed(exc_type: Union[type[BaseException], None], *_: object) -> None:
            if exc_type is None:
                # Warning filters and log handlers can block, so we keep them off the event loop
                await run_in_threadpool(self._warn)

        request_stack.push_async_exit(warn_unless_the_request_has_failed)

    def _warn(self, *, is_deferred: bool = True) -> None:
        message = (
            f"Cadwyn has dropped a background task of {self.route}: a dependency has added it while Cadwyn solved the "
            "dependencies of the latest version, and the endpoint doesn't declare a `BackgroundTasks` param. "
            "Cadwyn 10.0 will run such tasks after the response. To run them now, pass "
            f"`{_RUN_BACKGROUND_TASKS_FLAG}=True` to `Cadwyn()`. Dependencies run both for the request's version and "
            "for the latest version, so a task that a dependency adds in both runs will then run twice. To keep a "
            'single run, add it only when `current_dependency_solver` is "fastapi".'
        )
        if not is_deferred:
            message += (
                " Cadwyn couldn't defer this warning until FastAPI has sent the response, so it warns even if the "
                "request fails afterwards."
            )
        with self._lock:
            if self.has_warned:
                return
            # Nothing here may break the request, even if warnings are errors or logging fails
            try:
                warnings.warn(message, FutureWarning, stacklevel=1)
            except Exception:  # noqa: BLE001
                try:
                    _logger.warning(message)
                except Exception:  # noqa: BLE001
                    return
            self.has_warned = True
