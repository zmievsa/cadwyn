# NOTE: It's OK that any_string might not be correctly sortable such as v10 vs v9.
# we can simply remove waterfalling from any_string api version style.

import inspect
import re
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import date
from typing import Annotated, Any, Awaitable, Callable, Literal, assert_never, cast

import fastapi
from fastapi import Request, Response
from fastapi._compat import _normalize_errors
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, DispatchFunction, RequestResponseEndpoint
from starlette.types import ASGIApp

from cadwyn.structure.common import VersionType
from cadwyn.structure.versions import VersionBundle

VersionGetterC = Callable[[Request], str | None]
VersionValidatorC = Callable[[str], VersionType]
VersionDependencyFactoryC = Callable[[], Callable[..., Any]]

APIVersionLocation = Literal["header", "url"]
APIVersionStyle = Literal["date", "any_string"]


class HeaderVersionGetter:
    __slots__ = ("api_version_parameter_name",)

    def __init__(self, *, api_version_parameter_name: str, **kwargs: Any) -> None:
        super().__init__()
        self.api_version_parameter_name = api_version_parameter_name

    def __call__(self, request: Request) -> str | None:
        return request.headers.get(self.api_version_parameter_name)


class URLVersionGetter:
    __slots__ = ("possible_version_values", "url_version_regex")

    def __init__(self, *, possible_version_values: set[str], **kwargs: Any) -> None:
        super().__init__()
        self.possible_version_values = possible_version_values
        self.url_version_regex = re.compile(f"/({'|'.join(re.escape(v) for v in possible_version_values)})/")

    def __call__(self, request: Request) -> str | None:
        if m := self.url_version_regex.search(request.url.path):
            version = m.group(1)
            if version in self.possible_version_values:
                return version


class App:
    def __init__(
        self,
        versions: VersionBundle,
        api_version_location: APIVersionLocation = "header",
        api_version_style: APIVersionStyle = "date",
        api_version_parameter_name: str = "X-API-VERSION",
        api_version_default_value: str | None | Callable[[Request], Awaitable[str]] = None,
    ):
        super().__init__()

        if api_version_location == "header":
            getter = HeaderVersionGetter(api_version_parameter_name=api_version_parameter_name)
            fastapi_depends_class = fastapi.Header
        elif api_version_location == "url":
            getter = URLVersionGetter(possible_version_values=versions._version_values)
            fastapi_depends_class = fastapi.Path
        else:
            assert_never(api_version_location)
        if api_version_style == "date":
            default_version_example = "2022-11-16"
            validation_data_type = date
        elif api_version_style == "any_string":
            default_version_example = next(iter(versions._version_values))
            validation_data_type = str
        else:
            assert_never(default_version_example)

        self.api_version_dependency = _generate_api_version_dependency(
            api_version_parameter_name=api_version_parameter_name,
            version_example=default_version_example,
            fastapi_depends_class=fastapi_depends_class,
            validation_data_type=validation_data_type,
        )


def _generate_api_version_dependency(
    *,
    api_version_parameter_name: str,
    version_example: str,
    fastapi_depends_class: Callable[..., Any],
    validation_data_type: type,
):
    def api_version_dependency(**kwargs: Any):
        # TODO: What do I return?
        return next(iter(kwargs.values()))

    api_version_dependency.__signature__ = inspect.Signature(
        parameters=[
            inspect.Parameter(
                api_version_parameter_name.replace("-", "_"),
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[validation_data_type, fastapi_depends_class(examples=[version_example])],
                default=version_example,
            ),
        ],
    )
    return api_version_dependency


class VersionPickingMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_version_header_name: str,
        api_version_var: ContextVar[VersionType] | ContextVar[VersionType | None],
        default_response_class: type[Response] = JSONResponse,
        dispatch: DispatchFunction | None = None,
    ) -> None:
        super().__init__(app, dispatch)
        self.api_version_header_name = api_version_header_name
        self.api_version_var = api_version_var
        self.default_response_class = default_response_class
        # We use the dependant to apply fastapi's validation to the header, making validation at middleware level
        # consistent with validation and route level.
        self.version_header_validation_dependant = get_dependant(
            path="",
            call=_generate_api_version_dependency(api_version_header_name, "2000-08-23"),
        )

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ):
        # We handle api version at middleware level because if we try to add a Dependency to all routes, it won't work:
        # we use this header for routing so the user will simply get a 404 if the header is invalid.
        api_version: date | None = None
        if self.api_version_header_name in request.headers:
            async with AsyncExitStack() as async_exit_stack:
                solved_result = await solve_dependencies(
                    request=request,
                    dependant=self.version_header_validation_dependant,
                    async_exit_stack=async_exit_stack,
                    embed_body_fields=False,
                )
                if solved_result.errors:
                    return self.default_response_class(status_code=422, content=_normalize_errors(solved_result.errors))
                api_version = cast(date, solved_result.values[self.api_version_header_name.replace("-", "_")])
                self.api_version_var.set(api_version.isoformat())

        response = await call_next(request)

        if api_version is not None:
            # We return it because we will be returning the **matched** version, not the requested one.
            response.headers[self.api_version_header_name] = api_version.isoformat()

        return response
