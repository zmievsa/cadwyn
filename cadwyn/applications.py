import dataclasses
import warnings
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from datetime import date
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional, Union, cast

import fastapi
from fastapi import APIRouter, FastAPI, HTTPException, routing
from fastapi.datastructures import Default
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.openapi.utils import get_openapi
from fastapi.params import Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.utils import generate_unique_id
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import Lifespan
from typing_extensions import Self, assert_never, deprecated

from cadwyn._utils import DATACLASS_SLOTS, same_definition_as_in
from cadwyn.changelogs import CadwynChangelogResource, _generate_changelog
from cadwyn.exceptions import CadwynStructureError
from cadwyn.middleware import (
    APIVersionFormat,
    APIVersionLocation,
    HeaderVersionManager,
    URLVersionManager,
    VersionPickingMiddleware,
    _generate_api_version_dependency,
)
from cadwyn.route_generation import generate_versioned_routers
from cadwyn.routing import _RootCadwynAPIRouter
from cadwyn.structure import VersionBundle

if TYPE_CHECKING:
    from cadwyn.structure.common import VersionType

CURR_DIR = Path(__file__).resolve()
logger = getLogger(__name__)


@dataclasses.dataclass(**DATACLASS_SLOTS)
class FakeDependencyOverridesProvider:
    dependency_overrides: dict[Callable[..., Any], Callable[..., Any]]


class Cadwyn(FastAPI):
    _templates = Jinja2Templates(directory=CURR_DIR.parent / "static")

    def __init__(
        self,
        *,
        versions: VersionBundle,
        api_version_header_name: Annotated[
            Union[str, None],
            deprecated(
                "api_version_header_name is deprecated and will be removed in the future. "
                "Use api_version_parameter_name instead."
            ),
        ] = None,
        api_version_location: APIVersionLocation = "custom_header",
        api_version_format: APIVersionFormat = "date",
        api_version_parameter_name: str = "x-api-version",
        api_version_default_value: Union[str, None, Callable[[Request], Awaitable[str]]] = None,
        api_version_title: Optional[str] = None,
        api_version_description: Optional[str] = None,
        versioning_middleware_class: type[VersionPickingMiddleware] = VersionPickingMiddleware,
        changelog_url: Union[str, None] = "/changelog",
        include_changelog_url_in_schema: bool = True,
        debug: bool = False,
        title: str = "FastAPI",
        summary: Union[str, None] = None,
        description: str = "",
        version: str = "0.1.0",
        openapi_url: Union[str, None] = "/openapi.json",
        openapi_tags: Union[list[dict[str, Any]], None] = None,
        servers: Union[list[dict[str, Union[str, Any]]], None] = None,
        dependencies: Union[Sequence[Depends], None] = None,
        default_response_class: type[Response] = JSONResponse,
        redirect_slashes: bool = True,
        routes: Union[list[BaseRoute], None] = None,
        docs_url: Union[str, None] = "/docs",
        redoc_url: Union[str, None] = "/redoc",
        swagger_ui_oauth2_redirect_url: Union[str, None] = "/docs/oauth2-redirect",
        swagger_ui_init_oauth: Union[dict[str, Any], None] = None,
        middleware: Union[Sequence[Middleware], None] = None,
        exception_handlers: (
            Union[
                dict[
                    Union[int, type[Exception]],
                    Callable[[Request, Any], Coroutine[Any, Any, Response]],
                ],
                None,
            ]
        ) = None,
        on_startup: Union[Sequence[Callable[[], Any]], None] = None,
        on_shutdown: Union[Sequence[Callable[[], Any]], None] = None,
        lifespan: Union[Lifespan[Self], None] = None,
        terms_of_service: Union[str, None] = None,
        contact: Union[dict[str, Union[str, Any]], None] = None,
        license_info: Union[dict[str, Union[str, Any]], None] = None,
        openapi_prefix: str = "",
        root_path: str = "",
        root_path_in_servers: bool = True,
        responses: Union[dict[Union[int, str], dict[str, Any]], None] = None,
        callbacks: Union[list[BaseRoute], None] = None,
        webhooks: Union[APIRouter, None] = None,
        deprecated: Union[bool, None] = None,
        include_in_schema: bool = True,
        swagger_ui_parameters: Union[dict[str, Any], None] = None,
        generate_unique_id_function: Callable[[routing.APIRoute], str] = Default(  # noqa: B008
            generate_unique_id
        ),
        separate_input_output_schemas: bool = True,
        **extra: Any,
    ) -> None:
        self.versions = versions
        self._dependency_overrides_provider = FakeDependencyOverridesProvider({})
        self._cadwyn_initialized = False

        if api_version_header_name is not None:
            warnings.warn(
                "api_version_header_name is deprecated and will be removed in the future. "
                "Use api_version_parameter_name instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            api_version_parameter_name = api_version_header_name
        if api_version_default_value is not None and api_version_location == "path":
            raise CadwynStructureError(
                "You tried to pass an api_version_default_value while putting the API version in Path. "
                "This is not currently supported by Cadwyn. "
                "Please, open an issue on our github if you'd like to have it."
            )

        super().__init__(
            debug=debug,
            title=title,
            summary=summary,
            description=description,
            version=version,
            openapi_tags=openapi_tags,
            servers=servers,
            dependencies=dependencies,
            default_response_class=default_response_class,
            redirect_slashes=redirect_slashes,
            openapi_url=None,
            docs_url=None,
            redoc_url=None,
            swagger_ui_oauth2_redirect_url=swagger_ui_oauth2_redirect_url,
            swagger_ui_init_oauth=swagger_ui_init_oauth,
            middleware=middleware,
            exception_handlers=exception_handlers,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            lifespan=lifespan,
            terms_of_service=terms_of_service,
            contact=contact,
            license_info=license_info,
            openapi_prefix=openapi_prefix,
            root_path=root_path,
            root_path_in_servers=root_path_in_servers,
            responses=responses,
            callbacks=callbacks,
            webhooks=webhooks,
            deprecated=deprecated,
            include_in_schema=include_in_schema,
            swagger_ui_parameters=swagger_ui_parameters,
            generate_unique_id_function=generate_unique_id_function,
            separate_input_output_schemas=separate_input_output_schemas,
            **extra,
        )

        self._versioned_webhook_routers: dict[VersionType, APIRouter] = {}
        self._latest_version_router = APIRouter(dependency_overrides_provider=self._dependency_overrides_provider)

        self.changelog_url = changelog_url
        self.include_changelog_url_in_schema = include_changelog_url_in_schema

        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.redoc_url = redoc_url

        self._kwargs_to_router: dict[str, Any] = {
            "routes": routes,
            "redirect_slashes": redirect_slashes,
            "dependency_overrides_provider": self,
            "on_startup": on_startup,
            "on_shutdown": on_shutdown,
            "lifespan": lifespan,
            "default_response_class": default_response_class,
            "dependencies": dependencies,
            "callbacks": callbacks,
            "deprecated": deprecated,
            "include_in_schema": include_in_schema,
            "responses": responses,
            "generate_unique_id_function": generate_unique_id_function,
        }
        self.api_version_format = api_version_format
        self.api_version_parameter_name = api_version_parameter_name
        self.api_version_pythonic_parameter_name = api_version_parameter_name.replace("-", "_")
        self.api_version_title = api_version_title
        self.api_version_description = api_version_description
        if api_version_location == "custom_header":
            self._api_version_manager = HeaderVersionManager(api_version_parameter_name=api_version_parameter_name)
            self._api_version_fastapi_depends_class = fastapi.Header
        elif api_version_location == "path":
            self._api_version_manager = URLVersionManager(possible_version_values=self.versions._version_values_set)
            self._api_version_fastapi_depends_class = fastapi.Path
        else:
            assert_never(api_version_location)
        # TODO: Add a test validating the error message when there are no versions
        default_version_example = next(iter(self.versions._version_values_set))
        if api_version_format == "date":
            self.api_version_validation_data_type = date
        elif api_version_format == "string":
            self.api_version_validation_data_type = str
        else:
            assert_never(default_version_example)
        self.router: _RootCadwynAPIRouter = _RootCadwynAPIRouter(  # pyright: ignore[reportIncompatibleVariableOverride]
            **self._kwargs_to_router,
            api_version_parameter_name=api_version_parameter_name,
            api_version_var=self.versions.api_version_var,
            api_version_format=api_version_format,
        )
        unversioned_router = APIRouter(**self._kwargs_to_router)
        self._add_utility_endpoints(unversioned_router)
        self._add_default_versioned_routers()
        self.include_router(unversioned_router)
        self.add_middleware(
            versioning_middleware_class,
            api_version_parameter_name=api_version_parameter_name,
            api_version_manager=self._api_version_manager,
            api_version_var=self.versions.api_version_var,
            api_version_default_value=api_version_default_value,
        )
        if self.api_version_format == "date" and (
            sorted(self.versions.versions, key=lambda v: v.value, reverse=True) != list(self.versions.versions)
        ):
            raise CadwynStructureError(
                "Versions are not sorted correctly. Please sort them in descending order.",
            )

    @same_definition_as_in(FastAPI.__call__)
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not self._cadwyn_initialized:
            self._cadwyn_initialize()
        self.__call__ = super().__call__
        await self.__call__(scope, receive, send)

    def _cadwyn_initialize(self) -> None:
        generated_routers = generate_versioned_routers(
            self._latest_version_router,
            webhooks=self.webhooks,
            versions=self.versions,
        )
        for version, router in generated_routers.endpoints.items():
            self._add_versioned_routers(router, version=version)

        for version, router in generated_routers.webhooks.items():
            self._versioned_webhook_routers[version] = router
        self._cadwyn_initialized = True

    def _add_default_versioned_routers(self) -> None:
        for version in self.versions:
            self.router.versioned_routers[version.value] = APIRouter(**self._kwargs_to_router)

    @property
    def dependency_overrides(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        # TODO: Remove this approach as it is no longer necessary
        # This is only necessary because we cannot send self to versioned router generator
        # because it takes a deepcopy of the router and self.versions.head_schemas_package was a module
        # which couldn't be copied.
        return self._dependency_overrides_provider.dependency_overrides

    @dependency_overrides.setter
    def dependency_overrides(  # pyright: ignore[reportIncompatibleVariableOverride]
        self,
        value: dict[Callable[..., Any], Callable[..., Any]],
    ) -> None:
        self._dependency_overrides_provider.dependency_overrides = value

    def generate_changelog(self) -> CadwynChangelogResource:
        return _generate_changelog(self.versions, self.router)

    def _add_utility_endpoints(self, unversioned_router: APIRouter):
        if self.changelog_url is not None:
            unversioned_router.add_api_route(
                path=self.changelog_url,
                endpoint=self.generate_changelog,
                response_model=CadwynChangelogResource,
                methods=["GET"],
                include_in_schema=self.include_changelog_url_in_schema,
            )

        if self.openapi_url is not None:
            unversioned_router.add_route(
                path=self.openapi_url,
                endpoint=self.openapi_jsons,
                include_in_schema=False,
            )
            if self.docs_url is not None:
                unversioned_router.add_route(
                    path=self.docs_url,
                    endpoint=self.swagger_dashboard,
                    include_in_schema=False,
                )
                if self.swagger_ui_oauth2_redirect_url:

                    async def swagger_ui_redirect(req: Request) -> HTMLResponse:
                        return (
                            get_swagger_ui_oauth2_redirect_html()  # pragma: no cover # unimportant right now but # TODO
                        )

                    self.add_route(
                        self.swagger_ui_oauth2_redirect_url,
                        swagger_ui_redirect,
                        include_in_schema=False,
                    )
            if self.redoc_url is not None:
                unversioned_router.add_route(
                    path=self.redoc_url,
                    endpoint=self.redoc_dashboard,
                    include_in_schema=False,
                )

    def generate_and_include_versioned_routers(self, *routers: APIRouter) -> None:
        for router in routers:
            self._latest_version_router.include_router(router)

    async def openapi_jsons(self, req: Request) -> JSONResponse:
        version = req.query_params.get("version") or req.headers.get(self.router.api_version_parameter_name)

        if version in self.router.versioned_routers:
            routes = self.router.versioned_routers[version].routes
            formatted_version = version
        elif version == "unversioned" and self._there_are_public_unversioned_routes():
            routes = self.router.unversioned_routes
            formatted_version = "unversioned"
        else:
            raise HTTPException(
                status_code=404,
                detail=f"OpenApi file of with version `{version}` not found",
            )

        # Add root path to servers when mounted as sub-app or proxy is used
        urls = (server_data.get("url") for server_data in self.servers)
        server_urls = {url for url in urls if url}
        root_path = self._extract_root_path(req)
        if root_path and root_path not in server_urls and self.root_path_in_servers:
            self.servers.insert(0, {"url": root_path})

        webhook_routes = None
        if version in self._versioned_webhook_routers:
            webhook_routes = self._versioned_webhook_routers[version].routes

        return JSONResponse(
            get_openapi(
                title=self.title,
                version=formatted_version,
                openapi_version=self.openapi_version,
                description=self.description,
                summary=self.summary,
                terms_of_service=self.terms_of_service,
                contact=self.contact,
                license_info=self.license_info,
                routes=routes,
                webhooks=webhook_routes,
                tags=self.openapi_tags,
                servers=self.servers,
            )
        )

    def _there_are_public_unversioned_routes(self):
        return any(isinstance(route, Route) and route.include_in_schema for route in self.router.unversioned_routes)

    async def swagger_dashboard(self, req: Request) -> Response:
        version = req.query_params.get("version")

        if version:
            root_path = self._extract_root_path(req)
            openapi_url = root_path + f"{self.openapi_url}?version={version}"
            oauth2_redirect_url = self.swagger_ui_oauth2_redirect_url
            if oauth2_redirect_url:
                oauth2_redirect_url = root_path + oauth2_redirect_url
            return get_swagger_ui_html(
                openapi_url=openapi_url,
                title=f"{self.title} - Swagger UI",
                oauth2_redirect_url=oauth2_redirect_url,
                init_oauth=self.swagger_ui_init_oauth,
                swagger_ui_parameters=self.swagger_ui_parameters,
            )
        return self._render_docs_dashboard(req, cast("str", self.docs_url))

    async def redoc_dashboard(self, req: Request) -> Response:
        version = req.query_params.get("version")

        if version:
            root_path = self._extract_root_path(req)
            openapi_url = root_path + f"{self.openapi_url}?version={version}"
            return get_redoc_html(openapi_url=openapi_url, title=f"{self.title} - ReDoc")

        return self._render_docs_dashboard(req, docs_url=cast("str", self.redoc_url))

    def _extract_root_path(self, req: Request):
        return req.scope.get("root_path", "").rstrip("/")

    def _render_docs_dashboard(self, req: Request, docs_url: str):
        base_host = str(req.base_url).rstrip("/")
        root_path = self._extract_root_path(req)
        base_url = base_host + root_path
        table = {version: f"{base_url}{docs_url}?version={version}" for version in self.router.versions}
        if self._there_are_public_unversioned_routes():
            table |= {"unversioned": f"{base_url}{docs_url}?version=unversioned"}
        return self._templates.TemplateResponse(
            "docs.html",
            {"request": req, "table": table},
        )

    @deprecated("Use generate_and_include_versioned_routers and VersionBundle versions instead")
    def add_header_versioned_routers(
        self,
        first_router: APIRouter,
        *other_routers: APIRouter,
        header_value: str,
    ) -> list[BaseRoute]:
        """Add all routes from routers to be routed using header_value and return the added routes"""
        try:
            date.fromisoformat(header_value)
        except ValueError as e:
            raise ValueError("header_value should be in ISO 8601 format") from e

        return self._add_versioned_routers(first_router, *other_routers, version=header_value)

    def _add_versioned_routers(
        self, first_router: APIRouter, *other_routers: APIRouter, version: str
    ) -> list[BaseRoute]:
        added_routes: list[BaseRoute] = []
        if version not in self.router.versioned_routers:  # pragma: no branch
            self.router.versioned_routers[version] = APIRouter(**self._kwargs_to_router)

        versioned_router = self.router.versioned_routers[version]
        if self.openapi_url is not None:  # pragma: no branch
            versioned_router.add_route(
                path=self.openapi_url,
                endpoint=self.openapi_jsons,
                include_in_schema=False,
            )
            added_routes.append(versioned_router.routes[-1])

        added_route_count = 0
        for router in (first_router, *other_routers):
            self.router.versioned_routers[version].include_router(
                router,
                dependencies=[
                    Depends(
                        _generate_api_version_dependency(
                            api_version_pythonic_parameter_name=self.api_version_pythonic_parameter_name,
                            default_value=version,
                            fastapi_depends_class=self._api_version_fastapi_depends_class,
                            validation_data_type=self.api_version_validation_data_type,
                            title=self.api_version_title,
                            description=self.api_version_description,
                        )
                    )
                ],
            )
            added_route_count += len(router.routes)

        added_routes.extend(versioned_router.routes[-added_route_count:])
        self.router.routes.extend(added_routes)

        return added_routes
