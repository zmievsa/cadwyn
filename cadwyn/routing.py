import bisect
from collections.abc import Sequence
from contextvars import ContextVar
from functools import cached_property
from logging import getLogger
from typing import Any

from fastapi.routing import APIRouter
from starlette.datastructures import URL
from starlette.responses import RedirectResponse
from starlette.routing import BaseRoute, Match
from starlette.types import Receive, Scope, Send

from cadwyn._utils import same_definition_as_in
from cadwyn.structure.common import VersionType

_logger = getLogger(__name__)


class _RootCadwynAPIRouter(APIRouter):
    def __init__(
        self,
        *args: Any,
        api_version_parameter_name: str,
        api_version_var: ContextVar[str | None],
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.versioned_routers: dict[VersionType, APIRouter] = {}
        self.api_version_parameter_name = api_version_parameter_name.lower()
        self.api_version_var = api_version_var
        self.unversioned_routes: list[BaseRoute] = []

    async def pick_version(self, version: VersionType) -> list[BaseRoute]:
        """Pick the versioned routes for the given version in case we failed to pick a concrete version"""
        return []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if "router" not in scope:  # pragma: no cover
            scope["router"] = self

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return

        version = self.api_version_var.get(None)

        # if version is None, then it's an unversioned request and we need to use the unversioned routes
        # if there will be a value, we search for the most suitable version
        if not version:
            routes = self.unversioned_routes
        elif version in self.versioned_routers:
            routes = self.versioned_routers[version].routes
        else:
            routes = await self.pick_version(version)
        await self.process_request(scope=scope, receive=receive, send=send, routes=routes)

    @cached_property
    def versions(self):
        # intentionally not sorting them because arbitrary string versioning style
        # does not guarantee that strings will be correctly sortable. For example, v10 < v9
        return list(self.versioned_routers.keys())

    @same_definition_as_in(APIRouter.add_api_route)
    def add_api_route(self, *args: Any, **kwargs: Any):
        super().add_api_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_definition_as_in(APIRouter.add_route)
    def add_route(self, *args: Any, **kwargs: Any):
        super().add_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_definition_as_in(APIRouter.add_api_websocket_route)
    def add_api_websocket_route(self, *args: Any, **kwargs: Any):  # pragma: no cover
        super().add_api_websocket_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    @same_definition_as_in(APIRouter.add_websocket_route)
    def add_websocket_route(self, *args: Any, **kwargs: Any):  # pragma: no cover
        super().add_websocket_route(*args, **kwargs)
        self.unversioned_routes.append(self.routes[-1])

    async def process_request(self, scope: Scope, receive: Receive, send: Send, routes: Sequence[BaseRoute]) -> None:
        # It's a copy-paste from starlette.routing.Router
        # but in this version self.routes were replaced with routes from the function arguments

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


class _RootCadwynDateAPIRouter(_RootCadwynAPIRouter):
    """Root router of the FastAPI app when using date based versioning.

    It will be used to route the requests to the correct versioned route
    based on the headers.

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

    @cached_property
    def min_routes_version(self):
        return min(self.versions)

    def find_closest_date_but_not_new(self, request_version: VersionType) -> VersionType:
        index = bisect.bisect_left(self.versions, request_version)
        # as bisect_left returns the index where to insert item x in list a, assuming a is sorted
        # we need to get the previous item and that will be a match
        return self.versions[index - 1]

    async def pick_version(self, version: VersionType) -> list[BaseRoute]:
        if self.min_routes_version > version:
            # then the request version is older that the oldest route we have
            _logger.info(
                "Request version is older than the oldest version. No route can match this version",
                extra={
                    "oldest_version": self.min_routes_version,
                    "request_version": version,
                },
            )
            return []
        version_chosen = self.find_closest_date_but_not_new(version)
        _logger.info(
            "Partial match. The endpoint with a lower version was selected for the API call",
            extra={
                "version_chosen": version_chosen,
                "request_version": version,
            },
        )
        return self.versioned_routers[version_chosen].routes
