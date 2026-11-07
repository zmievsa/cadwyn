# NOTE: It's OK that any_string might not be correctly sortable such as v10 vs v9.
# we can simply remove waterfalling from any_string api version style.

import inspect
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Annotated, Any, Literal, Optional, Protocol, Union

import fastapi
from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import _CachedRequest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cadwyn._internal.context_vars import DEFAULT_API_VERSION_VAR
from cadwyn.structure.common import VersionType


class VersionManager(Protocol):
    def get(self, request: Request) -> Union[str, None]: ...


VersionValidatorC = Callable[[str], VersionType]
VersionDependencyFactoryC = Callable[[], Callable[..., Any]]

APIVersionLocation = Literal["custom_header", "path"]
APIVersionFormat = Literal["date", "string"]


class HeaderVersionManager:
    __slots__ = ("api_version_parameter_name",)

    def __init__(self, *, api_version_parameter_name: str) -> None:
        super().__init__()
        self.api_version_parameter_name = api_version_parameter_name

    def get(self, request: Request) -> Union[str, None]:
        return request.headers.get(self.api_version_parameter_name)


class URLVersionManager:
    __slots__ = ("possible_version_values", "url_version_regex")

    def __init__(self, *, possible_version_values: set[str]) -> None:
        super().__init__()
        self.possible_version_values = possible_version_values
        self.url_version_regex = re.compile(f"/({'|'.join(re.escape(v) for v in possible_version_values)})/")

    def get(self, request: Request) -> Union[str, None]:
        if m := self.url_version_regex.search(request.url.path):
            return m.group(1)
        return None


def _generate_api_version_dependency(
    *,
    api_version_pythonic_parameter_name: str,
    default_value: str,
    fastapi_depends_class: Callable[..., Any],
    validation_data_type: Any,
    title: Optional[str] = None,
    description: Optional[str] = None,
):
    def api_version_dependency(**kwargs: Any) -> Any:
        # TODO: What do I return?
        return next(iter(kwargs.values()))

    api_version_dependency.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
        parameters=[
            inspect.Parameter(
                api_version_pythonic_parameter_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[
                    validation_data_type,
                    fastapi_depends_class(
                        openapi_examples={"default": {"value": default_value}},
                        title=title,
                        description=description,
                    ),
                ],
                default=default_value if fastapi_depends_class != fastapi.Path else inspect.Signature.empty,
            )
        ]
    )
    return api_version_dependency


class VersionPickingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_version_parameter_name: str,
        api_version_default_value: Union[str, Callable[[Request], Awaitable[str]], None],
        api_version_var: ContextVar[Union[VersionType, None]],
        api_version_manager: VersionManager,
    ) -> None:
        if hasattr(self, "dispatch"):
            raise TypeError(
                "VersionPickingMiddleware no longer supports dispatch overrides. "
                "Override the ASGI __call__(scope, receive, send) method instead."
            )
        self.app = app

        self.api_version_parameter_name = api_version_parameter_name
        self._api_version_manager = api_version_manager
        self.api_version_var = api_version_var
        self.api_version_default_value = api_version_default_value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Preserve body reads by a callable default, just as BaseHTTPMiddleware did.
        request = _CachedRequest(scope, receive)
        # We handle api version at middleware level because if we try to add a Dependency to all routes, it won't work:
        # we use this header for routing so the user will simply get a 404 if the header is invalid.
        api_version = self._api_version_manager.get(request)

        if api_version is None:
            if isinstance(self.api_version_default_value, str | None):
                api_version = self.api_version_default_value
            else:
                api_version = await self.api_version_default_value(request)
            DEFAULT_API_VERSION_VAR.set(api_version)

        self.api_version_var.set(api_version)

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start" and api_version is not None:
                # Return the matched version, which can differ from the requested one after waterfalling.
                headers = MutableHeaders(scope=message)
                headers[self.api_version_parameter_name] = scope.get("cadwyn.api_version", api_version)
            await send(message)

        await self.app(scope, request.wrapped_receive, send_with_version)
