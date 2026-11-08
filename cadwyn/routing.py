import bisect
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import date
from functools import cached_property
from logging import getLogger
from typing import Any, Union

import anyio
from fastapi.routing import APIRouter
from pydantic import TypeAdapter, ValidationError
from starlette import status
from starlette.datastructures import URL
from starlette.exceptions import WebSocketException
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.routing import BaseRoute, Match
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket
from typing_extensions import override

from cadwyn._internal.context_vars import DEFAULT_API_VERSION_VAR
from cadwyn._utils import same_method_definition_as_in
from cadwyn.exceptions import CadwynError
from cadwyn.middleware import (
    _PATH_VERSION_MATCH_SCOPE_KEY,
    APIVersionFormat,
    APIVersionLocation,
    HeaderVersionManager,
    URLVersionManager,
    _match_key,
)
from cadwyn.structure.common import VersionType
from cadwyn.structure.versions import _UPLOAD_CLOSE_TIMEOUT_SECONDS

_logger = getLogger(__name__)
_DATE_ADAPTER = TypeAdapter(date)


class _RootCadwynAPIRouter(APIRouter):
    def __init__(
        self,
        *args: Any,
        api_version_parameter_name: str,
        api_version_var: ContextVar[Union[str, None]],
        api_version_format: APIVersionFormat,
        api_version_location: APIVersionLocation = "custom_header",
        api_version_default_value: str | Callable[[Request], Awaitable[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.versioned_routers: dict[VersionType, APIRouter] = {}
        self.api_version_parameter_name = api_version_parameter_name.lower()
        self.api_version_var = api_version_var
        self.unversioned_routes: list[BaseRoute] = []
        self.api_version_format = api_version_format
        # VersionPickingMiddleware gets the same manager, so HTTP requests and WebSockets read the version alike
        self.api_version_manager: HeaderVersionManager | URLVersionManager
        if api_version_location == "path":
            self.api_version_manager = URLVersionManager(
                api_version_parameter_name=api_version_parameter_name, versioned_routers=self.versioned_routers
            )
        else:
            self.api_version_manager = HeaderVersionManager(api_version_parameter_name=api_version_parameter_name)
        self._api_version_default_value = api_version_default_value

    async def _get_routes_from_closest_suitable_version(self, version: VersionType) -> list[BaseRoute]:
        """Pick the versioned routes for the given version in case we failed to pick a concrete version

        If the app has two versions: 2022-01-02 and 2022-01-05, and the request header
        is 2022-01-03, then the request will be routed to 2022-01-02 version as it the closest
        version, but lower than the request header.

        Exact match is always preferred over partial match and a request will never be
        matched to the higher versioned route.

        We implement routing like this because it is extremely convenient with microservice
        architecture. For example, imagine that you have two Cadwyn services: Payables and Receivables,
        each defining its own API versions. Payables service might contain 10 versions while receivables
        service might contain only 2 versions because it didn't need as many breaking changes.
        If a client requests a version that does not exist in receivables -- we will just waterfall
        to some earlier version, making receivables behavior consistent even if API keeps getting new versions.
        """
        if self.api_version_format == "date":
            index = bisect.bisect_left(self.versions, version)
            # That's when we try to get a version earlier than the earliest possible version
            if index == 0:
                return []
            picked_version = self.versions[index - 1]
            self.api_version_var.set(picked_version)
            # as bisect_left returns the index where to insert item x in list a, assuming a is sorted
            # we need to get the previous item and that will be a match
            return self.versioned_routers[picked_version].routes
        return []  # pragma: no cover # This should not be possible

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if "router" not in scope:  # pragma: no cover
            scope["router"] = self

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return
        version = self.api_version_var.get(None)
        default_version_that_was_picked = DEFAULT_API_VERSION_VAR.get(None)

        # if version is None, then it's an unversioned request and we need to use the unversioned routes
        # if there will be a value, we search for the most suitable version
        if not version:
            routes = self.unversioned_routes
        elif version in self.versioned_routers:
            routes = self.versioned_routers[version].routes
        else:
            routes = await self._get_routes_from_closest_suitable_version(version)
        scope["cadwyn.api_version"] = self.api_version_var.get(None)
        if default_version_that_was_picked:
            # We add unversioned routes to versioned routes because otherwise unversioned routes
            # will be completely unavailable when a default version is passed. So routes such as
            # /docs will not be accessible at all.

            # We use this order because if versioned routes go first and there is a versioned route that is
            # the same as an unversioned route -- the unversioned one becomes impossible to match.
            routes = self.unversioned_routes + routes
        # FastAPI closes the uploads of its forms with this stack after the response. `VersionPickingMiddleware`
        # (a `BaseHTTPMiddleware`) cancels the request when the client disconnects, which would interrupt that, so we
        # exit the stack here, shielded from the cancellation, like `async with` would. FastAPI then exits the empty
        # stack as usual
        file_stack = scope.get("fastapi_middleware_astack")
        if not isinstance(file_stack, AsyncExitStack):  # E.g. the router is used as an ASGI app on its own
            await self.process_request(scope=scope, receive=receive, send=send, routes=routes)
            return
        try:
            await self.process_request(scope=scope, receive=receive, send=send, routes=routes)
        except BaseException as exc:
            suppressed = False
            with anyio.move_on_after(_UPLOAD_CLOSE_TIMEOUT_SECONDS, shield=True):
                suppressed = await file_stack.__aexit__(type(exc), exc, exc.__traceback__)
            if not suppressed:
                raise
        else:
            with anyio.move_on_after(_UPLOAD_CLOSE_TIMEOUT_SECONDS, shield=True):
                await file_stack.__aexit__(None, None, None)

    async def _handle_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope.setdefault("router", self)
        version = self.api_version_var.get(None)
        version_token = self.api_version_var.set(version)
        default_token = DEFAULT_API_VERSION_VAR.set(DEFAULT_API_VERSION_VAR.get(None))
        try:
            if version is None:
                # These sockets were already reachable before WebSocket version picking existed,
                # even with an invalid header or a Request-based default. Keep them unversioned.
                if any(route.matches(scope)[0] == Match.FULL for route in self.unversioned_routes):
                    scope["cadwyn.api_version"] = None
                    await self.process_request(scope, receive, send, self.unversioned_routes)
                    return
                version = self._pick_websocket_version(WebSocket(scope, receive, send))
                self.api_version_var.set(version)

            if not version:
                routes = self.unversioned_routes
            elif version in self.versioned_routers:
                routes = self.versioned_routers[version].routes
            else:
                routes = await self._get_routes_from_closest_suitable_version(version)
            if not routes:
                raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "No matching API version")
            scope["cadwyn.api_version"] = self.api_version_var.get(None)
            if DEFAULT_API_VERSION_VAR.get(None):
                routes = self.unversioned_routes + routes
            await self.process_request(scope, receive, send, routes)
        finally:
            self.api_version_var.reset(version_token)
            DEFAULT_API_VERSION_VAR.reset(default_token)

    def _pick_websocket_version(self, websocket: WebSocket) -> str:
        # VersionPickingMiddleware is HTTP-only, so we pick the version with the same manager and rules here
        version = self.api_version_manager.get(websocket)
        if version is None:
            if callable(self._api_version_default_value):
                raise CadwynError(
                    "A callable api_version_default_value is unsupported for WebSocket connections; "
                    "send an explicit API version or use a constant default."
                )
            version = self._api_version_default_value
            DEFAULT_API_VERSION_VAR.set(version)
        if not version:
            raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "Missing API version")
        if self.api_version_format == "date":
            try:
                _DATE_ADAPTER.validate_python(version)
            except ValidationError as exc:
                raise WebSocketException(status.WS_1008_POLICY_VIOLATION, "Invalid API version") from exc
        return version

    @cached_property
    def versions(self):
        return sorted(self.versioned_routers.keys())

    @same_method_definition_as_in(APIRouter.add_api_route)
    @override
    def add_api_route(self, *args: Any, **kwargs: Any):
        super().add_api_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_method_definition_as_in(APIRouter.include_router)
    @override
    def include_router(self, *args: Any, **kwargs: Any):
        routes_before = len(self.routes)
        unversioned_routes_before = len(self.unversioned_routes)
        super().include_router(*args, **kwargs)
        if len(self.unversioned_routes) == unversioned_routes_before:  # pragma: no branch
            self.unversioned_routes.extend(self.routes[routes_before:])

    @same_method_definition_as_in(APIRouter.add_route)
    @override
    def add_route(self, *args: Any, **kwargs: Any):
        super().add_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_method_definition_as_in(APIRouter.mount)
    @override
    def mount(self, *args: Any, **kwargs: Any):
        super().mount(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_method_definition_as_in(APIRouter.add_api_websocket_route)
    @override
    def add_api_websocket_route(self, *args: Any, **kwargs: Any):  # pragma: no cover
        super().add_api_websocket_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_method_definition_as_in(APIRouter.add_websocket_route)
    @override
    def add_websocket_route(self, *args: Any, **kwargs: Any):  # pragma: no cover
        super().add_websocket_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    async def process_request(self, scope: Scope, receive: Receive, send: Send, routes: Sequence[BaseRoute]) -> None:
        # It's a copy-paste from starlette.routing.Router
        # but in this version self.routes were replaced with routes from the function arguments

        # URLVersionManager has already found the route while picking the version from the path
        path_version_match = scope.pop(_PATH_VERSION_MATCH_SCOPE_KEY, None)
        if (
            path_version_match is not None
            and path_version_match.routes is routes
            and path_version_match.key == _match_key(scope)
        ):
            scope.update(path_version_match.child_scope)
            await path_version_match.route.handle(scope, receive, send)
            return None

        partial = None
        partial_scope = {}
        for route in routes:
            # Determine if any route matches the incoming scope,
            # and hand over to the matching route if found.
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                await route.handle(scope, receive, send)
                return None
            if match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            #  Handle partial matches. These are cases where an endpoint is
            # able to handle the request, but is not a preferred option.
            # We use this in particular to deal with "405 Method Not Allowed".
            scope.update(partial_scope)
            return await partial.handle(scope, receive, send)

        if scope["type"] == "http" and self.redirect_slashes and scope["path"] != "/":
            redirect_scope = dict(scope)
            if scope["path"].endswith("/"):
                redirect_scope["path"] = redirect_scope["path"].rstrip("/")
            else:
                redirect_scope["path"] = redirect_scope["path"] + "/"

            for route in routes:
                match, child_scope = route.matches(redirect_scope)
                if match != Match.NONE:
                    redirect_url = URL(scope=redirect_scope)
                    response = RedirectResponse(url=str(redirect_url))
                    await response(scope, receive, send)
                    return None

        return await self.default(scope, receive, send)

    def extend_routes(self, routes: Sequence[BaseRoute]) -> None:
        self.routes.extend(routes)
        self._mark_routes_changed()
