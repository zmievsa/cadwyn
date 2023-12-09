from collections.abc import Callable, Coroutine, Sequence
from types import ModuleType
from typing import Any

from fastapi import APIRouter, routing
from fastapi.datastructures import Default
from fastapi.params import Depends
from fastapi.utils import generate_unique_id
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute
from starlette.types import Lifespan
from typing_extensions import Self
from verselect import HeaderRoutingFastAPI

from cadwyn.routing import generate_versioned_routers
from cadwyn.structure import VersionBundle


class Cadwyn(HeaderRoutingFastAPI):
    def __init__(
        self,
        *,
        versions: VersionBundle,
        latest_schemas_package: ModuleType,
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
        default_response_class: type[Response] = Default(JSONResponse),  # noqa: B008
        redirect_slashes: bool = True,
        docs_url: str | None = "/docs",
        redoc_url: None = None,
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
        super().__init__(
            api_version_header_name=api_version_header_name,
            api_version_var=versions.api_version_var,
            debug=debug,
            title=title,
            summary=summary,
            description=description,
            version=version,
            openapi_url=openapi_url,
            openapi_tags=openapi_tags,
            servers=servers,
            dependencies=dependencies,
            default_response_class=default_response_class,
            redirect_slashes=redirect_slashes,
            docs_url=docs_url,
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
        self.versions = versions
        self.latest_schemas_package = latest_schemas_package

    def generate_and_include_versioned_routers(self, *routers: APIRouter) -> None:
        root_router = APIRouter()
        for router in routers:
            root_router.include_router(router)
        router_versions = generate_versioned_routers(
            root_router,
            versions=self.versions,
            latest_schemas_package=self.latest_schemas_package,
        )
        for version, router in router_versions.items():
            self.add_header_versioned_routers(router, header_value=version.isoformat())
