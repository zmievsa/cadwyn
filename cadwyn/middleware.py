# NOTE: It's OK that any_string might not be correctly sortable such as v10 vs v9.
# we can simply remove waterfalling from any_string api version style.

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from typing import Annotated, Any, Literal, Optional, Protocol, Union

import fastapi
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, DispatchFunction, RequestResponseEndpoint
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.routing import BaseRoute, Match, Router
from starlette.types import ASGIApp, Scope
from typing_extensions import override

from cadwyn._internal.context_vars import DEFAULT_API_VERSION_VAR
from cadwyn.exceptions import CadwynError
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

    def get(self, request: HTTPConnection) -> Union[str, None]:
        return request.headers.get(self.api_version_parameter_name)


class URLVersionManager:
    """Reads the API version from the version path parameter of the route that the URL matches

    Other path parameters, such as a tenant ID, can hold a value that looks like a version, so we only accept
    a version if the route that this version's router would pick has that version in its version parameter.
    """

    __slots__ = ("api_version_path_parameter_name", "versioned_routers")

    def __init__(self, *, api_version_parameter_name: str, versioned_routers: Mapping[str, Router]) -> None:
        super().__init__()
        self.api_version_path_parameter_name = api_version_parameter_name.replace("-", "_")
        self.versioned_routers = versioned_routers

    def get(self, request: HTTPConnection) -> Union[str, None]:
        scope = request.scope
        versions = [
            version
            for version, router in self.versioned_routers.items()
            if version in scope["path"] and self._get_path_version(router, scope) == version
        ]
        if len(versions) > 1:
            raise CadwynError(
                f"The path {scope['path']!r} matches routes of several API versions ({', '.join(versions)}) "
                "with the API version in different positions, so Cadwyn can't pick one. "
                "Keep the API version path parameter in the same position in every version."
            )
        return versions[0] if versions else None

    def _get_path_version(self, router: Router, scope: Scope) -> Union[str, None]:
        # The same search as in _RootCadwynAPIRouter.process_request: a full match, a partial match (405),
        # and then a match with the trailing slash added or removed (a redirect)
        if (child_scope := _match_route(router.routes, scope)) is None:
            if scope["type"] != "http" or not router.redirect_slashes or scope["path"] == "/":
                return None
            path = scope["path"]
            redirect_scope = {**scope, "path": path.rstrip("/") if path.endswith("/") else path + "/"}
            if (child_scope := _match_route(router.routes, redirect_scope, redirect=True)) is None:
                return None
        return child_scope.get("path_params", {}).get(self.api_version_path_parameter_name)


def _match_route(routes: Sequence[BaseRoute], scope: Scope, *, redirect: bool = False) -> Union[Scope, None]:
    partial = None
    for route in routes:
        match, child_scope = route.matches(scope)
        if match == Match.FULL or (redirect and match == Match.PARTIAL):
            return child_scope
        if match == Match.PARTIAL and partial is None:
            partial = child_scope
    return partial


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


class VersionPickingMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_version_parameter_name: str,
        api_version_default_value: Union[str, Callable[[Request], Awaitable[str]], None],
        api_version_var: ContextVar[Union[VersionType, None]],
        api_version_manager: VersionManager,
        dispatch: Union[DispatchFunction, None] = None,
    ) -> None:
        super().__init__(app, dispatch)

        self.api_version_parameter_name = api_version_parameter_name
        self._api_version_manager = api_version_manager
        self.api_version_var = api_version_var
        self.api_version_default_value = api_version_default_value

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
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
        response = await call_next(request)

        if api_version is not None:
            # We return it because we will be returning the **matched** version, not the requested one.
            # In date-based versioning with waterfalling, it makes sense.
            response.headers[self.api_version_parameter_name] = request.scope.get("cadwyn.api_version", api_version)

        return response
