# NOTE: It's OK that any_string might not be correctly sortable such as v10 vs v9.
# we can simply remove waterfalling from any_string api version style.

import bisect
import inspect
import re
from contextvars import ContextVar
from typing import Annotated, Any, Awaitable, Callable, Literal

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, DispatchFunction, RequestResponseEndpoint
from starlette.types import ASGIApp

from cadwyn.structure.common import VersionType

VersionGetterC = Callable[[Request], str | None]
VersionValidatorC = Callable[[str], VersionType]
VersionDependencyFactoryC = Callable[[], Callable[..., Any]]

APIVersionLocation = Literal["custom_header", "url"]
APIVersionStyle = Literal["date", "any_string"]


class HeaderVersionGetter:
    __slots__ = ("api_version_parameter_name",)

    def __init__(self, *, api_version_parameter_name: str) -> None:
        super().__init__()
        self.api_version_parameter_name = api_version_parameter_name

    def __call__(self, request: Request) -> str | None:
        return request.headers.get(self.api_version_parameter_name)


class URLVersionGetter:
    __slots__ = ("possible_version_values", "url_version_regex")

    def __init__(self, *, possible_version_values: set[str]) -> None:
        super().__init__()
        self.possible_version_values = possible_version_values
        self.url_version_regex = re.compile(f"/({'|'.join(re.escape(v) for v in possible_version_values)})/")

    def __call__(self, request: Request) -> str | None:
        if m := self.url_version_regex.search(request.url.path):
            version = m.group(1)
            if version in self.possible_version_values:
                return version


def _generate_api_version_dependency(
    *,
    api_version_pythonic_parameter_name: str,
    default_value: str,
    fastapi_depends_class: Callable[..., Any],
    validation_data_type: Any,
):
    def api_version_dependency(**kwargs: Any):
        # TODO: What do I return?
        return next(iter(kwargs.values()))

    api_version_dependency.__signature__ = inspect.Signature(
        parameters=[
            inspect.Parameter(
                api_version_pythonic_parameter_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Annotated[
                    validation_data_type, fastapi_depends_class(openapi_examples={"default": {"value": default_value}})
                ],
            ),
        ],
    )
    return api_version_dependency


class VersionPickingMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_version_parameter_name: str,
        api_version_default_value: str | None | Callable[[Request], Awaitable[str]],
        api_version_var: ContextVar[VersionType | None],
        api_version_getter: VersionGetterC,
        api_version_possible_values: list[str],
        api_version_style: APIVersionStyle,
        dispatch: DispatchFunction | None = None,
    ) -> None:
        super().__init__(app, dispatch)

        self.api_version_parameter_name = api_version_parameter_name
        self.api_version_getter = api_version_getter
        self.api_version_var = api_version_var
        self.api_version_default_value = api_version_default_value
        self.api_version_possible_values = api_version_possible_values
        self.api_version_possible_values_set = set(api_version_possible_values)
        self.is_date_based_versioning = api_version_style == "date"

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ):
        # We handle api version at middleware level because if we try to add a Dependency to all routes, it won't work:
        # we use this header for routing so the user will simply get a 404 if the header is invalid.
        api_version = self.api_version_getter(request)

        if api_version is None:
            if callable(self.api_version_default_value):
                api_version = await self.api_version_default_value(request)
            else:
                api_version = self.api_version_default_value
        if (
            self.is_date_based_versioning
            and api_version is not None
            and api_version not in self.api_version_possible_values_set
        ):
            api_version = self.find_closest_date_but_not_new(api_version)
        self.api_version_var.set(api_version)

        response = await call_next(request)

        if api_version is not None:
            # We return it because we will be returning the **matched** version, not the requested one.
            # In date-based versioning with waterfalling, it makes sense.
            response.headers[self.api_version_parameter_name] = api_version

        return response

    def find_closest_date_but_not_new(self, request_version: VersionType) -> VersionType | None:
        index = bisect.bisect_left(self.api_version_possible_values, request_version)
        # That's when we try to get a version earlier than the earliest possible version
        if index == 0:
            return None
        # as bisect_left returns the index where to insert item x in list a, assuming a is sorted
        # we need to get the previous item and that will be a match
        return self.api_version_possible_values[index - 1]
