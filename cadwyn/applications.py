from collections.abc import Callable, Coroutine, Sequence
from datetime import date
from logging import getLogger
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from fastapi import APIRouter, FastAPI, HTTPException, routing
from fastapi.datastructures import Default
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.params import Depends
from fastapi.templating import Jinja2Templates
from fastapi.utils import generate_unique_id
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import Lifespan
from typing_extensions import Self, deprecated

from cadwyn.middleware import HeaderVersioningMiddleware, _get_api_version_dependency
from cadwyn.route_generation import generate_versioned_routers
from cadwyn.routing import _RootHeaderAPIRouter
from cadwyn.structure import VersionBundle

CURR_DIR = Path(__file__).resolve()
logger = getLogger(__name__)


class Cadwyn(FastAPI):
    _templates = Jinja2Templates(directory=CURR_DIR.parent / "static")

    def __init__(
        self,
        *,
        versions: VersionBundle,
        api_version_header_name: str = "x-api-version",
        debug: bool = False,
        title: str = "FastAPI",
        summary: str | None = None,
        description: str = "",
        version: str = "0.1.0",
        openapi_url: str | None = "/openapi.json",
        openapi_tags: list[dict[str, Any]] | None = None,
        servers: list[dict[str, str | Any]] | None = None,
        dependencies: Sequence[Depends] | None = None,
        default_response_class: type[Response] = JSONResponse,
        redirect_slashes: bool = True,
        routes: list[BaseRoute] | None = None,
        docs_url: str | None = "/docs",
        redoc_url: str | None = "/redoc",
        swagger_ui_oauth2_redirect_url: str | None = "/docs/oauth2-redirect",
        swagger_ui_init_oauth: dict[str, Any] | None = None,
        middleware: Sequence[Middleware] | None = None,
        exception_handlers: dict[int | type[Exception], Callable[[Request, Any], Coroutine[Any, Any, Response]]]
        | None = None,
        on_startup: Sequence[Callable[[], Any]] | None = None,
        on_shutdown: Sequence[Callable[[], Any]] | None = None,
        lifespan: Lifespan[Self] | None = None,
        terms_of_service: str | None = None,
        contact: dict[str, str | Any] | None = None,
        license_info: dict[str, str | Any] | None = None,
        openapi_prefix: str = "",
        root_path: str = "",
        root_path_in_servers: bool = True,
        responses: dict[int | str, dict[str, Any]] | None = None,
        callbacks: list[BaseRoute] | None = None,
        webhooks: APIRouter | None = None,
        deprecated: bool | None = None,
        include_in_schema: bool = True,
        swagger_ui_parameters: dict[str, Any] | None = None,
        generate_unique_id_function: Callable[[routing.APIRoute], str] = Default(generate_unique_id),  # noqa: B008
        separate_input_output_schemas: bool = True,
        **extra: Any,
    ) -> None:
        self.versions = versions
        # TODO: Remove argument entirely in any major version.
        latest_schemas_package = extra.pop("latest_schemas_package", None) or self.versions.head_schemas_package
        self.versions.head_schemas_package = latest_schemas_package
        self._latest_schemas_package = cast(ModuleType, latest_schemas_package)

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
        self.router: _RootHeaderAPIRouter = _RootHeaderAPIRouter(  # pyright: ignore[reportIncompatibleVariableOverride]
            routes=self.routes,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            default_response_class=default_response_class,
            dependencies=dependencies,
            callbacks=callbacks,
            deprecated=deprecated,
            responses=responses,
            api_version_header_name=api_version_header_name,
            api_version_var=self.versions.api_version_var,
            lifespan=lifespan,
        )
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.redoc_url = redoc_url
        self.swaggers = {}

        unversioned_router = APIRouter(routes=routes)
        self._add_openapi_endpoints(unversioned_router)
        self.add_unversioned_routers(unversioned_router)
        self.add_middleware(
            HeaderVersioningMiddleware,
            api_version_header_name=self.router.api_version_header_name,
            api_version_var=self.versions.api_version_var,
            default_response_class=default_response_class,
        )

    @property  # pragma: no cover
    @deprecated("It is going to be deleted in the future. Use VersionBundle.head_schemas_package instead")
    def latest_schemas_package(self):
        return self._latest_schemas_package

    @latest_schemas_package.setter  # pragma: no cover
    @deprecated("It is going to be deleted in the future. Use VersionBundle.head_schemas_package instead")
    def latest_schemas_package(self, value: ModuleType | None):
        self._latest_schemas_package = value

    def _add_openapi_endpoints(self, unversioned_router: APIRouter):
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
            if self.redoc_url is not None:
                unversioned_router.add_route(
                    path=self.redoc_url,
                    endpoint=self.redoc_dashboard,
                    include_in_schema=False,
                )

    def generate_and_include_versioned_routers(self, *routers: APIRouter) -> None:
        root_router = APIRouter()
        for router in routers:
            root_router.include_router(router)
        router_versions = generate_versioned_routers(
            root_router,
            versions=self.versions,
        )
        for version, router in router_versions.items():
            self.add_header_versioned_routers(router, header_value=version.isoformat())

    def enrich_swagger(self):
        """
        This method goes through all header-based apps and collect a dict[openapi_version, openapi_json]

        For each route a `X-API-VERSION` header with value is added

        """
        unversioned_routes_openapi = get_openapi(
            title=self.title,
            version=self.version,
            openapi_version=self.openapi_version,
            description=self.description,
            terms_of_service=self.terms_of_service,
            contact=self.contact,
            license_info=self.license_info,
            routes=self.router.unversioned_routes,
            tags=self.openapi_tags,
            servers=self.servers,
        )
        if unversioned_routes_openapi["paths"]:
            self.swaggers["unversioned"] = unversioned_routes_openapi

        for header_value, routes in self.router.versioned_routes.items():
            header_value_str = header_value.isoformat()
            openapi = get_openapi(
                title=self.title,
                version=self.version,
                openapi_version=self.openapi_version,
                description=self.description,
                terms_of_service=self.terms_of_service,
                contact=self.contact,
                license_info=self.license_info,
                routes=routes,
                tags=self.openapi_tags,
                servers=self.servers,
            )
            # in current implementation we expect header_value to be a date
            self.swaggers[header_value_str] = openapi

    async def openapi_jsons(self, req: Request) -> JSONResponse:
        version = req.query_params.get("version") or req.headers.get(self.router.api_version_header_name)
        openapi_of_a_version = self.swaggers.get(version)
        if not openapi_of_a_version:
            raise HTTPException(
                status_code=404,
                detail=f"OpenApi file of with version `{version}` not found",
            )

        return JSONResponse(openapi_of_a_version)

    async def swagger_dashboard(self, req: Request) -> Response:
        return self._render_docs_dashboard_or_concrete_verssion(get_swagger_ui_html, req, cast(str, self.docs_url))

    async def redoc_dashboard(self, req: Request) -> Response:
        return self._render_docs_dashboard_or_concrete_verssion(get_redoc_html, req, cast(str, self.redoc_url))

    def _render_docs_dashboard_or_concrete_verssion(self, render_docs: Callable[..., Any], req: Request, docs_url: str):
        base_url = str(req.base_url).rstrip("/")
        version = req.query_params.get("version")

        if version:
            return render_docs(
                openapi_url=f"{self.openapi_url}?version={version}",
                title="Swagger UI",
            )
        return self._templates.TemplateResponse(
            "docs.html",
            {
                "request": req,
                "table": {version: f"{base_url}{docs_url}?version={version}" for version in sorted(self.swaggers)},
            },
        )

    def add_header_versioned_routers(
        self,
        first_router: APIRouter,
        *other_routers: APIRouter,
        header_value: str,
    ) -> list[BaseRoute]:
        """Add all routes from routers to be routed using header_value and return the added routes"""
        try:
            header_value_as_dt = date.fromisoformat(header_value)
        except ValueError as e:
            raise ValueError("header_value should be in ISO 8601 format") from e

        if header_value_as_dt not in self.router.versioned_routes:  # pragma: no branch
            self.router.versioned_routes[header_value_as_dt] = []
            if self.openapi_url is not None:  # pragma: no branch
                self.router.versioned_routes[header_value_as_dt].append(
                    Route(path=self.openapi_url, endpoint=self.openapi_jsons, include_in_schema=False)
                )

        added_routes: list[BaseRoute] = []
        for router in (first_router, *other_routers):
            added_route_count = len(router.routes)

            self.include_router(
                router,
                dependencies=[Depends(_get_api_version_dependency(self.router.api_version_header_name, header_value))],
            )
            added_routes.extend(self.routes[len(self.routes) - added_route_count :])
        for route in added_routes:
            self.router.versioned_routes[header_value_as_dt].append(route)

        self.enrich_swagger()
        return added_routes

    def add_unversioned_routers(self, *routers: APIRouter):
        for router in routers:
            self.include_router(router)
            self.router.unversioned_routes.extend(router.routes)
        self.enrich_swagger()

    def add_unversioned_routes(self, *routes: Route):
        self.router.unversioned_routes.extend(routes)
