import asyncio
import copy
import dataclasses
import functools
import importlib
import inspect
import itertools
import json
import re
import sys
import typing
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import Enum, auto
from types import GenericAlias
from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
    Generic,
    Literal,
    NewType,
    Optional,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    get_args,
)
from uuid import UUID

import fastapi.params
import pytest
import svcs
from annotated_types import Ge, Len, MinLen
from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Response,
    Security,
    UploadFile,
)
from fastapi.routing import APIRoute, _EffectiveRouteContext
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, SecurityScopes
from fastapi.security.http import HTTPBasic
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from inline_snapshot import snapshot
from pydantic import (
    AfterValidator,
    AllowInfNan,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    Strict,
    StringConstraints,
    conint,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings
from pytest_fixture_classes import fixture_class
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route
from typing_extensions import Any, TypeAliasType, TypeVarTuple, Unpack, override

from cadwyn import VersionBundle, VersionedAPIRouter
from cadwyn._utils import _callable_name
from cadwyn.applications import Cadwyn
from cadwyn.dependencies import current_dependency_solver
from cadwyn.exceptions import (
    CadwynError,
    InvalidGenerationInstructionError,
    RouterGenerationError,
    RouterPathParamsModifiedError,
)
from cadwyn.route_generation import copy_route, generate_versioned_routers
from cadwyn.schema_generation import _UNRESOLVED, _get_value_of_type_alias_with_args, generate_versioned_models
from cadwyn.structure import Version, convert_request_to_next_version_for, endpoint, schema
from cadwyn.structure.data import RequestInfo, ResponseInfo, convert_response_to_previous_version_for
from cadwyn.structure.enums import enum
from cadwyn.structure.versions import HeadVersion, VersionChange
from tests._data.sqlalchemy_models import ImperativelyMappedModel, MappedChild, MappedModel, ModelFields
from tests._data.unversioned_schema_dir import UnversionedSchema2
from tests._data.unversioned_schema_dir.unversioned_schemas import UnversionedSchema1
from tests._data.unversioned_schemas import UnversionedSchema3
from tests._resources.utils import (
    FASTAPI_VALIDATES_DEFAULTS_OF_OMITTED_PARAMS,
    HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED,
)
from tests.conftest import (
    CreateVersionedApp,
    CreateVersionedClients,
    client,
    version_change,
)

Default = object()
Endpoint: TypeAlias = Callable[..., Awaitable[Any]]


class StrEnum(str, Enum):
    a = auto()


class EmptySchema(BaseModel):
    pass


class SchemaWithOneIntField(BaseModel):
    foo: int


class SchemaWithOnePydanticField(BaseModel):
    foo: SchemaWithOneIntField


def get_wrapped_endpoint(endpoint: Endpoint) -> Endpoint:
    while callable(wrapped_endpoint := getattr(endpoint, "__wrapped__", None)):
        endpoint = wrapped_endpoint
    return endpoint


def route_endpoint(route: object) -> Endpoint:
    assert isinstance(route, APIRoute)
    return route.endpoint


def dict_str_list_of(model: type[BaseModel]) -> GenericAlias:
    return GenericAlias(dict, (str, GenericAlias(list, (model,))))


def endpoints_equal(endpoint1: Endpoint, endpoint2: Endpoint) -> bool:
    endpoint1 = get_wrapped_endpoint(endpoint1)
    endpoint2 = get_wrapped_endpoint(endpoint2)
    return endpoint1 == endpoint2


@pytest.fixture
def test_path() -> str:
    return "/test/{hewwo}"


@pytest.fixture
def test_endpoint(router: VersionedAPIRouter, test_path: str, random_uuid: UUID) -> Endpoint:
    @router.get(test_path)
    async def test(hewwo: int):
        raise NotImplementedError

    return test


@fixture_class(name="create_versioned_api_routes")
class CreateVersionedAPIRoutes:
    create_versioned_app: CreateVersionedApp

    def __call__(
        self,
        *version_changes: type[VersionChange],
        router: Union[VersionedAPIRouter, None] = None,
    ) -> tuple[list[APIRoute], list[APIRoute]]:
        app = self.create_versioned_app(*version_changes, router=router)
        return (
            cast("list[APIRoute]", app.router.versioned_routers.get("2000-01-01", APIRouter()).routes),
            cast("list[APIRoute]", app.router.versioned_routers.get("2001-01-01", APIRouter()).routes),
        )


def test__endpoint_didnt_exist(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    routes_2000, routes_2001 = create_versioned_api_routes(version_change(endpoint(test_path, ["GET"]).didnt_exist))

    assert len(routes_2000) == 1
    assert routes_2000[0].path == "/openapi.json"
    assert len(routes_2001) == 2
    assert endpoints_equal(routes_2001[1].endpoint, test_endpoint)


def test__endpoint_existed(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    @router.post("/test")
    async def test_endpoint_post():
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(
            endpoint("/test", ["GET"]).existed,
        ),
    )

    assert len(routes_2000) == 3
    assert endpoints_equal(routes_2000[1].endpoint, test_endpoint)
    assert endpoints_equal(routes_2000[2].endpoint, test_endpoint_post)

    assert len(routes_2001) == 2
    assert endpoints_equal(routes_2001[1].endpoint, test_endpoint_post)


def test__endpoint_existed__route_from_included_router(
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    router = VersionedAPIRouter()
    child_router = VersionedAPIRouter()

    @child_router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    router.include_router(child_router)
    router.only_exists_in_older_versions(test_endpoint)

    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(endpoint("/test", ["GET"]).existed),
        router=router,
    )

    assert len(routes_2000) == 2
    assert endpoints_equal(routes_2000[1].endpoint, test_endpoint)
    assert len(routes_2001) == 1


def test__copy_route__copies_plain_route_when_effective_context_has_no_starlette_route():
    def plain_route(_request):
        return PlainTextResponse("plain")

    route = Route("/plain", plain_route, methods=["GET"])
    copied_route = copy_route(route, _EffectiveRouteContext(original_route=route))

    assert copied_route is not route
    assert copied_route.path == "/plain"
    assert copied_route.endpoint is plain_route
    assert copied_route.endpoint(None).body == b"plain"


def test__endpoint_existed__endpoint_removed_in_latest_but_never_restored__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Every route you mark with @VersionedAPIRouter.only_exists_in_older_versions must be restored in one "
            "of the older versions. Otherwise you just need to delete it altogether. The following routes have been "
            "marked with that decorator but were never restored: "
            "[APIRoute(path='/test', name='test_endpoint', methods=['GET'])]",
        ),
    ):
        create_versioned_api_routes(version_change())


def test__endpoint_existed__deleting_restoring_deleting_restoring_an_endpoint(
    router: VersionedAPIRouter,
    api_version_var: ContextVar[Union[str, None]],
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    class MyVersionChange3(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).existed]

    class MyVersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).didnt_exist]

    class MyVersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).existed]

    versions = VersionBundle(
        Version("2003-01-01", MyVersionChange3),
        Version("2002-01-01", MyVersionChange2),
        Version("2001-01-01", MyVersionChange1),
        Version("2000-01-01"),
        api_version_var=api_version_var,
    )
    routers = generate_versioned_routers(router, versions=versions)

    assert len(routers.endpoints["2003-01-01"].routes) == 0
    assert len(routers.endpoints["2002-01-01"].routes) == 1
    assert len(routers.endpoints["2001-01-01"].routes) == 0
    assert len(routers.endpoints["2000-01-01"].routes) == 1


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("path", "/wow/{hewwo}"),
        ("status_code", 204),
        ("tags", ["foo", "bar"]),
        ("summary", "my summary"),
        ("description", "my description"),
        ("response_description", "my response description"),
        ("deprecated", True),
        ("include_in_schema", False),
        ("name", "my name"),
        ("openapi_extra", {"my_openapi_extra": "openapi_extra"}),
        ("responses", {405: {"description": "hewwo"}, 500: {"description": "hewwo1"}}),
        ("methods", {"GET", "POST"}),
        ("operation_id", "my_operation_id"),
        ("response_class", FileResponse),
        (
            "generate_unique_id_function",
            lambda api_route: api_route.endpoint.__name__,
        ),  # pragma: no cover
    ],
)
def test__endpoint_had(
    attr: str,
    attr_value: Any,
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(
            endpoint(test_path, ["GET"]).had(**{attr: attr_value}),
        ),
    )

    assert len(routes_2000) == len(routes_2001) == 2
    assert getattr(routes_2000[1], attr) == attr_value
    assert getattr(routes_2001[1], attr) != attr_value


def test__endpoint_had_another_path_variable(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    with pytest.raises(RouterPathParamsModifiedError):
        create_versioned_api_routes(
            version_change(
                endpoint(test_path, ["GET"]).had(path="/test/{world}"),
            ),
        )


def test__endpoint_had__path_rename_with_api_version_in_prefix__should_not_raise():
    app_router = VersionedAPIRouter(prefix="/{api_version}")
    sub_router = VersionedAPIRouter()

    @sub_router.get("")
    async def list_items():
        return 83

    app_router.include_router(sub_router, prefix="/new-name")

    versions = VersionBundle(
        Version("v2", version_change(endpoint("/{api_version}/new-name", ["GET"]).had(path="/{api_version}/old-name"))),
        Version("v1"),
    )

    cadwyn_app = Cadwyn(
        versions=versions,
        api_version_location="path",
        api_version_parameter_name="api_version",
        api_version_format="string",
    )
    cadwyn_app.generate_and_include_versioned_routers(app_router)

    client_v2 = TestClient(cadwyn_app)
    assert client_v2.get("/v2/new-name").json() == 83
    assert client_v2.get("/v1/old-name").json() == 83


def test__versioned_router_generation__uses_routes_added_to_included_router_after_inclusion():
    api_router = VersionedAPIRouter(prefix="/api/v1")
    widgets_router = VersionedAPIRouter()

    def mark_included_router_dependency(response: Response):
        response.headers["x-included-router-dependency"] = "ran"

    api_router.include_router(
        widgets_router,
        prefix="/widgets",
        dependencies=[Depends(mark_included_router_dependency)],
    )

    class V2003(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [
            endpoint("/api/v1/widgets/{widget_id}/details", ["GET"]).had(path="/api/v1/widgets/{widget_id}"),
        ]

    class V2002(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [
            endpoint("/api/v1/widgets/{widget_id}", ["GET"]).had(path="/api/v1/items/{widget_id}"),
        ]

    app = Cadwyn(
        versions=VersionBundle(
            HeadVersion(),
            Version("2003-01-01", V2003),
            Version("2002-01-01", V2002),
            Version("2001-01-01"),
        )
    )
    app.generate_and_include_versioned_routers(api_router)

    @widgets_router.get("/{widget_id}/details")
    async def get_widget(widget_id: int):
        return {"widget_id": widget_id}

    with TestClient(app) as client:
        response_2003 = client.get("/api/v1/widgets/7/details", headers={"x-api-version": "2003-01-01"})
        response_2002 = client.get("/api/v1/widgets/7", headers={"x-api-version": "2002-01-01"})
        response_2001 = client.get("/api/v1/items/7", headers={"x-api-version": "2001-01-01"})

    for response in (response_2003, response_2002, response_2001):
        assert response.status_code == 200, response.json()
        assert response.json() == {"widget_id": 7}
        assert response.headers["x-included-router-dependency"] == "ran"


def test__versioned_router_generation__preserves_included_route_openapi_metadata():
    class Widget(BaseModel):
        name: str

    api_router = VersionedAPIRouter(prefix="/api/v1", tags=["api"], responses={400: {"description": "Bad API"}})
    widgets_router = VersionedAPIRouter(tags=["widgets"])

    api_router.include_router(
        widgets_router,
        prefix="/widgets",
        tags=["included"],
        responses={401: {"description": "Unauthorized"}},
        deprecated=True,
    )

    app = Cadwyn(
        versions=VersionBundle(
            HeadVersion(),
            Version("2003-01-01"),
            Version("2002-01-01"),
            Version("2001-01-01"),
        )
    )
    app.generate_and_include_versioned_routers(api_router)

    @widgets_router.post(
        "/{widget_id}",
        response_model=Widget,
        status_code=201,
        operation_id="createWidget",
        tags=["handler"],
        responses={404: {"description": "Missing widget"}},
        openapi_extra={"x-widget-operation": "create"},
    )
    async def create_widget(widget_id: int, body: Widget):
        return body

    with TestClient(app) as client:
        route_response = client.post(
            "/api/v1/widgets/7",
            headers={"x-api-version": "2003-01-01"},
            json={"name": "hammer"},
        )
        schema_response = client.get("/openapi.json?version=2003-01-01")

    assert route_response.status_code == 201, route_response.json()
    assert route_response.json() == {"name": "hammer"}
    assert schema_response.status_code == 200, schema_response.json()
    operation = schema_response.json()["paths"]["/api/v1/widgets/{widget_id}"]["post"]
    assert operation["deprecated"] is True
    assert operation["operationId"] == "createWidget"
    assert operation["x-widget-operation"] == "create"
    assert operation["tags"] == ["api", "included", "widgets", "handler"]
    assert operation["responses"]["201"]["description"] == "Successful Response"
    assert operation["responses"]["400"]["description"] == "Bad API"
    assert operation["responses"]["401"]["description"] == "Unauthorized"
    assert operation["responses"]["404"]["description"] == "Missing widget"


def test__get_openapi__older_version_with_params_without_examples__should_not_add_examples(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
):
    def get_page(page: int = Query()) -> int:
        raise NotImplementedError

    @router.post("/items/{item_id}")
    async def create_item(
        item_id: int = Path(),
        name: str = Query(),
        token: str = Header(),
        session: str = Cookie(),
        quantity: int = Body(),
        page: int = Depends(get_page),
    ) -> None:
        raise NotImplementedError

    app = create_versioned_app(version_change())

    with TestClient(app) as client:
        response = client.get("/openapi.json?version=2000-01-01")

    assert response.status_code == 200, response.json()
    operation = response.json()["paths"]["/items/{item_id}"]["post"]
    assert operation["parameters"] == [
        {"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer", "title": "Item Id"}},
        {"name": "name", "in": "query", "required": True, "schema": {"type": "string", "title": "Name"}},
        {"name": "page", "in": "query", "required": True, "schema": {"type": "integer", "title": "Page"}},
        {"name": "token", "in": "header", "required": True, "schema": {"type": "string", "title": "Token"}},
        {
            "name": "x-api-version",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "date", "default": "2000-01-01", "title": "X-Api-Version"},
            "examples": {"default": {"value": "2000-01-01"}},
        },
        {"name": "session", "in": "cookie", "required": True, "schema": {"type": "string", "title": "Session"}},
    ]
    assert operation["requestBody"] == {
        "required": True,
        "content": {"application/json": {"schema": {"type": "integer", "title": "Quantity"}}},
    }


def test__router_generation__schema_had_description__should_change_description_in_openapi_of_older_versions(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
):
    class User(BaseModel):
        """INTERNAL: head-only implementation note."""

        name: str

    class UserCreate(BaseModel):
        """INTERNAL: request note."""

        name: str

    @router.post("/users", response_model=User)
    async def create_user(user: UserCreate) -> Any:
        return user

    app = create_versioned_app(
        version_change(schema(User).had(description="A user."), schema(UserCreate).had(description=None)),
        version_change(schema(User).had(description="A user of the API.")),
    )

    with TestClient(app) as client:
        openapis = {
            version: client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
            for version in ("2002-01-01", "2001-01-01", "2000-01-01")
        }
        response = client.post("/users", json={"name": "Ada"}, headers={"x-api-version": "2000-01-01"})

    assert response.json() == {"name": "Ada"}
    assert {version: schemas["User"].get("description") for version, schemas in openapis.items()} == {
        "2002-01-01": "INTERNAL: head-only implementation note.",
        "2001-01-01": "A user of the API.",
        "2000-01-01": "A user.",
    }
    assert {version: schemas["UserCreate"].get("description") for version, schemas in openapis.items()} == {
        "2002-01-01": "INTERNAL: request note.",
        "2001-01-01": "INTERNAL: request note.",
        "2000-01-01": None,
    }


def test__router_generation__schema_had_description_in_head_version__should_change_description_in_every_version(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
):
    class User(BaseModel):
        """INTERNAL: head-only implementation note."""

        name: str

    @router.get("/users", response_model=User)
    async def get_user() -> Any:
        raise NotImplementedError

    app = create_versioned_app(
        version_change(schema(User).had(description="A user.")),
        head_version_changes=[version_change(schema(User).had(description="A user of the API."))],
    )

    with TestClient(app) as client:
        descriptions = {
            version: client.get(f"/openapi.json?version={version}")
            .json()["components"]["schemas"]["User"]
            .get("description")
            for version in ("2001-01-01", "2000-01-01")
        }

    assert descriptions == {"2001-01-01": "A user of the API.", "2000-01-01": "A user."}


def test__versioned_router_generation__hidden_included_route_still_routes_but_is_removed_from_openapi():
    api_router = VersionedAPIRouter(prefix="/api")
    internal_router = VersionedAPIRouter()
    api_router.include_router(internal_router, prefix="/internal", include_in_schema=False)

    app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2022-11-16")))
    app.generate_and_include_versioned_routers(api_router)

    @internal_router.get("/health")
    async def healthcheck():
        return {"ok": True}

    with TestClient(app) as client:
        route_response = client.get("/api/internal/health", headers={"x-api-version": "2022-11-16"})
        schema_response = client.get("/openapi.json?version=2022-11-16")

    assert route_response.status_code == 200, route_response.json()
    assert route_response.json() == {"ok": True}
    assert "/api/internal/health" not in schema_response.json()["paths"]


def test__versioned_router_generation__materializes_plain_routes_and_mounts_added_after_inclusion(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "hello.txt").write_text("Hello World")

    api_router = VersionedAPIRouter(prefix="/api")
    tools_router = VersionedAPIRouter()
    api_router.include_router(tools_router, prefix="/tools")

    app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2022-11-16")))
    app.generate_and_include_versioned_routers(api_router)

    async def plain_route(_request):
        return PlainTextResponse("plain")

    tools_router.add_route("/plain", plain_route, methods=["GET"])
    tools_router.mount("/static", StaticFiles(directory=static_dir), name="static")

    with TestClient(app) as client:
        plain_response = client.get("/api/tools/plain", headers={"x-api-version": "2022-11-16"})
        static_response = client.get("/api/tools/static/hello.txt", headers={"x-api-version": "2022-11-16"})

    assert plain_response.status_code == 200, plain_response.text
    assert plain_response.text == "plain"
    assert static_response.status_code == 200, static_response.text
    assert static_response.text == "Hello World"


def test__add_header_versioned_routers__uses_routes_added_to_included_router_after_cadwyn_inclusion():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    api_router = APIRouter(prefix="/api")
    items_router = APIRouter()

    def mark_included_router_dependency(response: Response):
        response.headers["x-included-router-dependency"] = "ran"

    api_router.include_router(
        items_router,
        prefix="/items",
        dependencies=[Depends(mark_included_router_dependency)],
    )

    with pytest.warns(DeprecationWarning, match="Use generate_and_include_versioned_routers"):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # This test verifies the legacy API.
            api_router,
            header_value="2022-11-16",
        )

    @items_router.get("/{item_id}")
    async def get_item(item_id: int):
        return {"item_id": item_id}

    with TestClient(app) as client:
        response = client.get("/api/items/11", headers={"x-api-version": "2022-11-16"})
        schema_response = client.get("/openapi.json?version=2022-11-16")

    assert response.status_code == 200, response.json()
    assert response.json() == {"item_id": 11}
    assert response.headers["x-included-router-dependency"] == "ran"
    assert "/api/items/{item_id}" in schema_response.json()["paths"]


def test__endpoint_had__another_path_with_the_other_migration_at_the_same_time__should_require_old_name(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/A")
    async def test_endpoint_post(body: list[str]):
        return body

    @router.put("/A")
    async def test_endpoint_put(body: list[str]):
        return body

    @convert_response_to_previous_version_for("/A", ["POST"])
    def response_migration(response: ResponseInfo):
        response.body.append("response")

    @convert_request_to_next_version_for("/A", ["POST"])
    def request_migration(request: RequestInfo):
        request.body.append("request")

    app = Cadwyn(
        versions=VersionBundle(
            Version(
                "2001-01-01",
                version_change(
                    endpoint("/A", ["POST"]).had(path="/B"),
                    request_migration=request_migration,
                    response_migration=response_migration,
                ),
            ),
            Version("2000-01-01"),
        )
    )
    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        assert client.post("/B", headers={"X-API-VERSION": "2000-01-01"}, json=[]).json() == ["request", "response"]
        assert client.post("/A", headers={"X-API-VERSION": "2001-01-01"}, json=[]).json() == []

        assert client.put("/A", headers={"X-API-VERSION": "2000-01-01"}, json=[]).json() == []
        assert client.put("/A", headers={"X-API-VERSION": "2001-01-01"}, json=[]).json() == []


def test__endpoint_had_dependencies(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(
            endpoint(test_path, ["GET"]).had(dependencies=[Depends(lambda: "hewwo")]),
        ),
    )

    assert len(routes_2000) == len(routes_2001) == 2
    assert len(routes_2000[1].dependencies) == 2
    dependency = routes_2000[1].dependencies[1].dependency
    assert dependency is not None
    assert dependency() == "hewwo"

    assert len(routes_2001[1].dependencies) == 1


def test__only_exists_in_older_versions__endpoint_is_not_a_route__should_raise_lookup_error(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
):
    with pytest.raises(
        LookupError,
        match=re.escape(
            'Route not found on endpoint: "test2". Are you sure it\'s a route and decorators are in the correct order?',
        ),
    ):

        @router.only_exists_in_older_versions
        async def test2():  # pragma: no branch
            raise NotImplementedError


def test__only_exists_in_older_versions__with_non_string_callable_name__should_use_class_name(
    router: VersionedAPIRouter,
):
    class EndpointWithNonStringName:
        @property
        def __name__(self) -> int:
            return 42

        async def __call__(self) -> None:
            raise NotImplementedError

    with pytest.raises(
        LookupError,
        match=re.escape(
            'Route not found on endpoint: "EndpointWithNonStringName". '
            "Are you sure it's a route and decorators are in the correct order?",
        ),
    ):
        router.only_exists_in_older_versions(EndpointWithNonStringName())


def test__only_exists_in_older_versions__applied_twice__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    with pytest.raises(
        CadwynError,
        match=re.escape('The route "test_endpoint" was already deleted. You can\'t delete it again.'),
    ):

        @router.only_exists_in_older_versions  # pragma: no branch
        @router.only_exists_in_older_versions
        @router.get("/test")
        async def test_endpoint():  # pragma: no branch
            raise NotImplementedError


def test__router_generation__changing_a_deleted_endpoint__should_raise_router_generation_error(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test():
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to change in "MyVersionChange" doesn\'t exist',
        ),
    ):
        create_versioned_app(version_change(endpoint("/test", ["GET"]).had(description="Hewwo")))


def test__router_generation__changing_a_non_existent_endpoint__should_raise_router_generation_error(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to change in "MyVersionChange" doesn\'t exist',
        ),
    ):
        create_versioned_app(version_change(endpoint("/test", ["GET"]).had(dependencies=[])))


def test__router_generation__re_creating_an_existing_endpoint__should_raise_router_generation_error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_app: CreateVersionedApp,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Endpoint \"['GET'] /test/{hewwo}\" you tried to restore in "
            '"MyVersionChange" already existed in a newer version',
        ),
    ):
        create_versioned_app(version_change(endpoint(test_path, ["GET"]).existed))


def test__router_generation__editing_an_endpoint_with_wrong_method__should_raise_error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_app: CreateVersionedApp,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape('Endpoint "[\'POST\'] /test/{hewwo}" you tried to change in "MyVersionChange" doesn\'t exist'),
    ):
        create_versioned_app(version_change(endpoint(test_path, ["POST"]).had(description="Hewwo")))


def test__router_generation__editing_an_endpoint_with_a_less_general_method__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    @router.route("/test/{hewwo}", methods=["GET", "POST"])
    async def test(hewwo: int):
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape('Endpoint "[\'GET\'] /test/{hewwo}" you tried to change in "MyVersionChange" doesn\'t exist'),
    ):
        create_versioned_app(version_change(endpoint("/test/{hewwo}", ["GET"]).had(description="Hewwo")))


def test__router_generation__editing_multiple_endpoints_with_same_route(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.api_route("/test/{hewwo}", methods=["GET", "POST"])
    async def test(hewwo: int):
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(
            endpoint("/test/{hewwo}", ["GET", "POST"]).had(description="Meaw"),
        ),
    )
    assert len(routes_2000) == len(routes_2001) == 2
    assert routes_2000[1].description == "Meaw"
    assert routes_2001[1].description == ""


def test__router_generation__editing_an_endpoint_with_a_more_general_method__should_raise_error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_app: CreateVersionedApp,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape('Endpoint "[\'POST\'] /test/{hewwo}" you tried to change in "MyVersionChange" doesn\'t exist'),
    ):
        create_versioned_app(version_change(endpoint(test_path, ["GET", "POST"]).had(description="Hewwo")))


def test__router_generation__editing_multiple_methods_of_multiple_endpoints__should_edit_both_methods(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.get("/test")
    async def test_get():
        raise NotImplementedError

    @router.post("/test")
    async def test_post():
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(
            endpoint("/test", ["GET", "POST"]).had(description="Meaw"),
        ),
    )
    assert routes_2000[1].description == "Meaw"
    assert routes_2000[2].description == "Meaw"

    assert routes_2001[1].description == ""
    assert routes_2001[2].description == ""


def test__router_generation__deleting_a_deleted_endpoint__should_raise_router_generation_error(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test():
        raise NotImplementedError

    # with insert_pytest_raises():
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to delete in "MyVersionChange" was already deleted in a '
            "newer version. If you really have two routes with the same paths and methods, please, use "
            '"endpoint(..., func_name=...)" to distinguish between them. '
            "Function names of endpoints that were already deleted: ['test']",
        ),
    ):
        create_versioned_app(version_change(endpoint("/test", ["GET"]).didnt_exist))


@pytest.mark.parametrize("delete_first", [True, False])
@pytest.mark.parametrize("route_index_to_delete_first", [0, 1])
def test__router_generation__restoring_deleted_routes_for_same_path_with_func_name__should_restore_only_one_route(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
    route_index_to_delete_first: int,
    delete_first: bool,
):
    @router.get("/test")
    async def test_get0():
        raise NotImplementedError

    @router.get("/test")
    async def test_get1():
        raise NotImplementedError

    routes = [test_get0, test_get1]
    route_to_delete_first = routes[route_index_to_delete_first]
    route_to_delete_second = routes[route_index_to_delete_first - 1]
    router.only_exists_in_older_versions(route_to_delete_first)
    instructions = [
        endpoint("/test", ["GET"], func_name=route_to_delete_first.__name__).existed,
        endpoint("/test", ["GET"], func_name=route_to_delete_second.__name__).didnt_exist,
    ]
    if delete_first:
        instructions.reverse()

    routes_2000, routes_2001 = create_versioned_api_routes(version_change(*instructions))

    assert len(routes_2000) == len(routes_2001) == 2
    assert endpoints_equal(routes_2000[1].endpoint, routes[route_index_to_delete_first])
    assert endpoints_equal(routes_2001[1].endpoint, routes[route_index_to_delete_first - 1])


def test__router_generation__changing_status_code_of_endpoint(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    @router.post("/test", status_code=201)
    async def test_get():
        return {"hello": "world"}

    app = create_versioned_app(
        version_change(
            endpoint("/test", ["POST"]).had(status_code=200),
        ),
    )
    client = TestClient(app)
    assert client.post("/test", headers={app.router.api_version_parameter_name: "2000-01-01"}).status_code == 200
    assert client.post("/test", headers={app.router.api_version_parameter_name: "2001-01-01"}).status_code == 201


@pytest.mark.parametrize("route_index_to_delete_first", [0, 1])
def test__router_generation__restoring_deleted_route_for_same_path_without_func_name__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
    route_index_to_delete_first: int,
):
    @router.get("/test")
    async def test_get0():
        raise NotImplementedError

    @router.get("/test")
    async def test_get1():
        raise NotImplementedError

    routes = [test_get0, test_get1]

    router.only_exists_in_older_versions(routes[route_index_to_delete_first])

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to delete in "MyVersionChange" was already deleted in '
            "a newer version. If you really have two routes with the same paths and methods, please, use "
            '"endpoint(..., func_name=...)" to distinguish between them. '
            f"Function names of endpoints that were already deleted: ['test_get{route_index_to_delete_first}']",
        ),
    ):
        create_versioned_api_routes(
            version_change(
                endpoint("/test", ["GET"]).didnt_exist,
                endpoint("/test", ["GET"]).existed,
            ),
        )


@pytest.fixture
def two_deleted_routes(router: VersionedAPIRouter):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_get0():
        raise NotImplementedError

    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_get1():
        raise NotImplementedError

    return test_get0, test_get1


def test__router_generation__restoring_two_deleted_routes_for_same_path__should_raise_error(
    two_deleted_routes: tuple[Endpoint, Endpoint],
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to restore in "MyVersionChange" has 2 applicable routes that '
            "could be restored. If you really have two routes with the same paths and methods, please, use "
            '"endpoint(..., func_name=...)" to distinguish between them. '
            "Function names of endpoints that can be restored: ['test_get1', 'test_get0']",
        ),
    ):
        create_versioned_api_routes(version_change(endpoint("/test", ["GET"]).existed))


def test__router_generation__restoring_routes_distinguishes_paths_and_methods(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def get_without_trailing_slash():
        raise NotImplementedError

    @router.only_exists_in_older_versions
    @router.get("/test/")
    async def get_with_trailing_slash():
        raise NotImplementedError

    @router.only_exists_in_older_versions
    @router.post("/test")
    async def post_without_trailing_slash():
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(version_change(endpoint("/test", ["GET", "POST"]).existed))

    assert len(routes_2000) == 4
    assert len(routes_2001) == 1
    restored_route_identities = set()
    for route in routes_2000[1:]:
        assert route.methods is not None
        restored_route_identities.add((route.path, frozenset(route.methods)))
    assert restored_route_identities == {
        ("/test", frozenset({"GET"})),
        ("/test/", frozenset({"GET"})),
        ("/test", frozenset({"POST"})),
    }


@pytest.mark.parametrize("route_index_to_restore_first", [0, 1])
def test__endpoint_existed__deleting_and_restoring_two_routes_for_the_same_endpoint(
    router: VersionedAPIRouter,
    api_version_var: ContextVar[Union[str, None]],
    two_deleted_routes: tuple[Endpoint, Endpoint],
    route_index_to_restore_first: int,
):
    route_to_restore_first = two_deleted_routes[route_index_to_restore_first]
    route_to_restore_second = two_deleted_routes[route_index_to_restore_first - 1]

    class MyVersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            endpoint("/test", ["GET"], func_name=_callable_name(route_to_restore_first)).existed,
        ]

    class MyVersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            endpoint("/test", ["GET"], func_name=_callable_name(route_to_restore_second)).existed,
        ]

    versions = VersionBundle(
        Version("2002-01-01", MyVersionChange2),
        Version("2001-01-01", MyVersionChange1),
        Version("2000-01-01"),
        api_version_var=api_version_var,
    )
    routers = generate_versioned_routers(router, versions=versions)

    assert len(routers.endpoints["2002-01-01"].routes) == 0
    assert len(routers.endpoints["2001-01-01"].routes) == 1
    assert len(routers.endpoints["2000-01-01"].routes) == 2

    assert endpoints_equal(route_endpoint(routers.endpoints["2001-01-01"].routes[0]), route_to_restore_first)
    assert {
        get_wrapped_endpoint(route_endpoint(routers.endpoints["2000-01-01"].routes[0])),
        get_wrapped_endpoint(route_endpoint(routers.endpoints["2000-01-01"].routes[1])),
    } == {
        route_to_restore_first,
        route_to_restore_second,
    }


def get_nested_field_type(annotation: Any) -> Union[type[BaseModel], None]:
    get_args(annotation)[1]
    first_generic_arg_of_second_generic_arg = get_args(get_args(annotation)[1])[0]
    its_fields = first_generic_arg_of_second_generic_arg.model_fields
    annotation_of_its_foo_field = its_fields["foo"].annotation
    assert annotation_of_its_foo_field is not None
    return annotation_of_its_foo_field.model_fields["foo"].annotation


def test__router_generation__re_creating_a_non_endpoint__should_raise_router_generation_error(
    create_versioned_app: CreateVersionedApp,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to restore in "MyVersionChange" wasn\'t among the deleted routes',
        ),
    ):
        create_versioned_app(version_change(endpoint("/test", ["GET"]).existed))


def test__router_generation__changing_attribute_to_the_same_value__should_raise_router_generation_error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_app: CreateVersionedApp,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Expected attribute "path" of endpoint "[\'GET\'] /test/{hewwo}" to be different in "MyVersionChange", but '
            "it was the same. It means that your version change has no effect on the attribute and can be removed.",
        ),
    ):
        create_versioned_app(version_change(endpoint(test_path, ["GET"]).had(path=test_path)))


def test__router_generation__non_api_route_added(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_app: CreateVersionedApp,
):
    @router.websocket("/test2")
    async def test_websocket():
        raise NotImplementedError

    app = create_versioned_app(version_change(endpoint(test_path, ["GET"]).didnt_exist))
    assert len(app.router.versioned_routers["2000-01-01"].routes) == 2
    assert len(app.router.versioned_routers["2001-01-01"].routes) == 3
    route = app.router.versioned_routers["2001-01-01"].routes[1]
    assert isinstance(route, APIRoute)
    assert endpoints_equal(route.endpoint, test_endpoint)


def test__router_generation__updating_response_model(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    @router.get(
        "/test",
        response_model=dict[str, list[SchemaWithOnePydanticField]],
    )
    async def test():
        raise NotImplementedError

    app = create_versioned_app(version_change(schema(SchemaWithOneIntField).field("foo").had(type=list[str])))
    schemas = generate_versioned_models(app.versions)

    routes_2000 = cast("list[APIRoute]", app.router.versioned_routers["2000-01-01"].routes)
    routes_2001 = cast("list[APIRoute]", app.router.versioned_routers["2001-01-01"].routes)

    assert len(routes_2000) == len(routes_2001) == 2

    schema_2000 = schemas["2000-01-01"][SchemaWithOnePydanticField]
    assert routes_2000[1].response_model == dict_str_list_of(schema_2000)

    schema_2001 = schemas["2001-01-01"][SchemaWithOnePydanticField]
    assert routes_2001[1].response_model == dict_str_list_of(schema_2001)

    assert get_nested_field_type(routes_2000[1].response_model) == list[str]
    assert get_nested_field_type(routes_2001[1].response_model) == int  # noqa: E721


def test__router_generation__response_model_with_union_return_type(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.get("/test", response_model=SchemaWithOneIntField)
    async def test() -> SchemaWithOneIntField | JSONResponse:
        return SchemaWithOneIntField(foo=83)

    clients = create_versioned_clients()
    response = clients["2000-01-01"].get("/test")
    assert response.status_code == 200
    assert response.json() == {"foo": 83}


def test__router_generation__using_unversioned_schema_from_versioned_base_dir__should_not_raise_error(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    module = importlib.import_module("tests._data.unversioned_schema_dir")

    @router.post("/testik")
    async def testik(body: module.UnversionedSchema2):
        raise NotImplementedError

    create_versioned_app()


def test__router_generation__updating_request_models(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    @router.get("/test")
    async def test(body: dict[str, list[SchemaWithOnePydanticField]]):
        raise NotImplementedError

    app = create_versioned_app(version_change(schema(SchemaWithOneIntField).field("foo").had(type=list[str])))
    schemas = generate_versioned_models(app.versions)

    routes_2000 = cast("list[APIRoute]", app.router.versioned_routers["2000-01-01"].routes)
    routes_2001 = cast("list[APIRoute]", app.router.versioned_routers["2001-01-01"].routes)
    assert len(routes_2000) == len(routes_2001) == 2

    body_param_2000 = routes_2000[1].dependant.body_params[0]
    schema_2000 = schemas["2000-01-01"][SchemaWithOnePydanticField]
    assert body_param_2000.field_info.annotation == dict_str_list_of(schema_2000)

    body_param_2001 = routes_2001[1].dependant.body_params[0]
    schema_2001 = schemas["2001-01-01"][SchemaWithOnePydanticField]
    assert body_param_2001.field_info.annotation == dict_str_list_of(schema_2001)

    assert get_nested_field_type(routes_2000[1].dependant.body_params[0].field_info.annotation) == list[str]
    assert get_nested_field_type(routes_2001[1].dependant.body_params[0].field_info.annotation) is int


def test__router_generation__updating_request_models_with_inheritance(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    class ParentSchema(BaseModel):
        foo: str
        bar: int

    class ChildSchema(ParentSchema):
        pass

    @router.get("/test")
    async def test(body: ChildSchema):
        raise NotImplementedError

    app = create_versioned_app(version_change(schema(ParentSchema).field("foo").didnt_exist))

    routes_2000 = cast("list[APIRoute]", app.router.versioned_routers["2000-01-01"].routes)
    routes_2001 = cast("list[APIRoute]", app.router.versioned_routers["2001-01-01"].routes)
    assert len(routes_2000) == len(routes_2001) == 2

    body_param_2000 = routes_2000[1].dependant.body_params[0]
    body_param_2001 = routes_2001[1].dependant.body_params[0]
    assert isinstance(body_param_2000.field_info.annotation, type)
    assert issubclass(body_param_2000.field_info.annotation, BaseModel)
    assert isinstance(body_param_2001.field_info.annotation, type)
    assert issubclass(body_param_2001.field_info.annotation, BaseModel)
    assert set(body_param_2000.field_info.annotation.model_fields) == {"bar"}
    assert set(body_param_2001.field_info.annotation.model_fields) == {"foo", "bar"}


def test__router_generation__using_unversioned_models(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    """Essentially legacy from codegen times"""

    @router.get("/test")
    async def test1(body: UnversionedSchema1):
        raise NotImplementedError

    @router.get("/test2")
    async def test2(body: UnversionedSchema2):
        raise NotImplementedError

    @router.get("/test3")
    async def test3(body: UnversionedSchema3):
        raise NotImplementedError

    app = create_versioned_app(version_change(schema(SchemaWithOneIntField).field("foo").had(type=list[str])))
    schemas = generate_versioned_models(app.versions)

    routes_2000 = cast("list[APIRoute]", app.router.versioned_routers["2000-01-01"].routes)
    routes_2001 = cast("list[APIRoute]", app.router.versioned_routers["2001-01-01"].routes)

    assert len(routes_2000) == len(routes_2001) == 4
    assert routes_2000[1].dependant.body_params[0].field_info.annotation is schemas["2000-01-01"][UnversionedSchema1]
    assert routes_2001[1].dependant.body_params[0].field_info.annotation is schemas["2001-01-01"][UnversionedSchema1]

    assert routes_2000[2].dependant.body_params[0].field_info.annotation is schemas["2000-01-01"][UnversionedSchema2]
    assert routes_2001[2].dependant.body_params[0].field_info.annotation is schemas["2001-01-01"][UnversionedSchema2]

    assert routes_2000[3].dependant.body_params[0].field_info.annotation is schemas["2000-01-01"][UnversionedSchema3]
    assert routes_2001[3].dependant.body_params[0].field_info.annotation is schemas["2001-01-01"][UnversionedSchema3]


def test__router_generation__using_newtype_and_union_typehints(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    newtype = NewType("newtype", str)

    @router.get("/test")
    async def test(param1: newtype = Body(), param2: Union[str, int] = Body()):
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(schema(SchemaWithOneIntField).field("foo").had(type=list[str])),
    )
    assert len(routes_2000) == len(routes_2001) == 2

    assert routes_2000[1].dependant.body_params[0].field_info.annotation is newtype
    assert routes_2001[1].dependant.body_params[0].field_info.annotation is newtype

    assert routes_2000[1].dependant.body_params[1].field_info.annotation == Union[str, int]
    assert routes_2001[1].dependant.body_params[1].field_info.annotation == Union[str, int]


def test__router_generation__using_uploadfile_typehint(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.get("/test")
    async def test(file: UploadFile):
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        version_change(schema(SchemaWithOneIntField).field("foo").had(type=list[str])),
    )
    assert len(routes_2000) == len(routes_2001) == 2
    # We are intentionally not checking anything here. Our goal is to validate that there is no exception


def test__router_generation__using_jsonvalue_typehint(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    # We fall into a recursion error if we use an unannotated syntax for this parameter
    @router.post("/test")
    async def test(param1: Annotated[JsonValue, Body()]):
        return param1

    clients = create_versioned_clients(
        version_change(endpoint("/test", ["POST"]).had(response_model=dict[str, str])),
    )
    assert clients["2000-01-01"].post("/test", json={"foo": "bar"}).json() == {"foo": "bar"}
    assert clients["2001-01-01"].post("/test", json={"foo": "bar"}).json() == {"foo": "bar"}


def test__router_generation__using_typealias_type_typehint(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    class MySchema(BaseModel):
        foo: str

    typealias = TypeAliasType("typealias", list[MySchema])

    @router.post("/test")
    async def test(param1: typealias = Body()):
        return param1

    clients = create_versioned_clients(
        version_change(schema(MySchema).field("foo").had(name="baz")),
    )
    assert clients["2000-01-01"].post("/test", json=[{"foo": "bar"}]).json() == snapshot(
        {
            "detail": [
                {
                    "type": "missing",
                    "loc": ["body", 0, "baz"],
                    "msg": "Field required",
                    "input": {
                        "foo": "bar",
                    },
                }
            ]
        }
    )
    assert clients["2001-01-01"].post("/test", json=[{"foo": "bar"}]).json() == [{"foo": "bar"}]


class ConstrainedFieldsInFieldDefaults(BaseModel):
    min_length_field: str = Field(min_length=1)
    max_length_field: str = Field(max_length=10)
    ge_field: int = Field(ge=0)
    le_field: int = Field(le=100)
    pattern_field: str = Field(pattern="^a")
    alias_field: str = Field(default="", title="Alias Field")
    description_field: str = Field(default="", description="New description")


class ConstrainedFieldsInAnnotated(BaseModel):
    min_length_field: Annotated[str, Field(min_length=1)]
    max_length_field: Annotated[str, Field(max_length=10)]
    ge_field: Annotated[int, Field(ge=0)]
    le_field: Annotated[int, Field(le=100)]
    pattern_field: Annotated[str, Field(pattern="^a")]
    alias_field: Annotated[str, Field(title="Alias Field")] = ""
    description_field: Annotated[str, Field(description="New description")] = ""


PAYLOAD_VALID_IN_ALL_VERSIONS = {
    "min_length_field": "aaaaaa",
    "max_length_field": "ab",
    "ge_field": 10,
    "le_field": 1,
    "pattern_field": "ab",
}


@pytest.fixture
def constrained_fields_router(router: VersionedAPIRouter) -> VersionedAPIRouter:
    @router.post("/field_defaults")
    async def field_defaults(payload: ConstrainedFieldsInFieldDefaults):
        return payload.model_dump()

    @router.post("/annotated")
    async def annotated(payload: ConstrainedFieldsInAnnotated):
        return payload.model_dump()

    return router


@pytest.mark.parametrize(
    ("path", "schema_name", "is_applied"),
    [
        ("/field_defaults", "ConstrainedFieldsInFieldDefaults", True),
        # Cadwyn 7.4.0 ignored had() for a constraint that the annotation's metadata sets (since pydantic 2.12).
        # Applying it would make the older versions reject the requests and responses that they have always
        # accepted, so it stays ignored
        ("/annotated", "ConstrainedFieldsInAnnotated", HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "old_attributes", "head_json_schema", "old_json_schema", "value_invalid_only_in_old_version"),
    [
        ("min_length_field", {"min_length": 5}, {"minLength": 1}, {"minLength": 5}, "ab"),
        ("max_length_field", {"max_length": 3}, {"maxLength": 10}, {"maxLength": 3}, "abcdef"),
        ("ge_field", {"ge": 5}, {"minimum": 0}, {"minimum": 5}, 1),
        ("le_field", {"le": 5}, {"maximum": 100}, {"maximum": 5}, 50),
        ("pattern_field", {"pattern": "^ab"}, {"pattern": "^a"}, {"pattern": "^ab"}, "ac"),
    ],
)
def test__router_generation__field_had_constraint_that_was_already_set__should_override_it_unless_annotation_sets_it(
    constrained_fields_router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    path: str,
    schema_name: str,
    is_applied: bool,
    field_name: str,
    old_attributes: dict[str, Any],
    head_json_schema: dict[str, object],
    old_json_schema: dict[str, object],
    value_invalid_only_in_old_version: object,
):
    clients = create_versioned_clients(
        version_change(
            schema(ConstrainedFieldsInFieldDefaults).field(field_name).had(**old_attributes),
            schema(ConstrainedFieldsInAnnotated).field(field_name).had(**old_attributes),
        ),
        router=constrained_fields_router,
    )
    payload = PAYLOAD_VALID_IN_ALL_VERSIONS | {field_name: value_invalid_only_in_old_version}

    for version, expected_json_schema, expected_status_code in [
        ("2001-01-01", head_json_schema, 200),
        ("2000-01-01", old_json_schema, 422) if is_applied else ("2000-01-01", head_json_schema, 200),
    ]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"][schema_name]["properties"][field_name]
        assert field_schema.items() >= expected_json_schema.items()

        assert client.post(path, json=PAYLOAD_VALID_IN_ALL_VERSIONS).status_code == 200
        assert client.post(path, json=payload).status_code == expected_status_code


@pytest.mark.parametrize(
    ("path", "schema_name"),
    [
        ("/field_defaults", "ConstrainedFieldsInFieldDefaults"),
        ("/annotated", "ConstrainedFieldsInAnnotated"),
    ],
)
def test__router_generation__field_had_alias_and_description__should_be_applied(
    constrained_fields_router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    path: str,
    schema_name: str,
):
    clients = create_versioned_clients(
        version_change(
            schema(ConstrainedFieldsInFieldDefaults).field("alias_field").had(alias="old_alias"),
            schema(ConstrainedFieldsInFieldDefaults).field("description_field").had(description="Old description"),
            schema(ConstrainedFieldsInAnnotated).field("alias_field").had(alias="old_alias"),
            schema(ConstrainedFieldsInAnnotated).field("description_field").had(description="Old description"),
        ),
        router=constrained_fields_router,
    )

    for version, alias, description in [
        ("2001-01-01", "alias_field", "New description"),
        ("2000-01-01", "old_alias", "Old description"),
    ]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        properties = openapi["components"]["schemas"][schema_name]["properties"]
        assert alias in properties
        assert properties["description_field"]["description"] == description


class ParentWithInheritedField(BaseModel):
    foo: str = Field(default="", description="Head description", max_length=10)
    bar: int = 0


class ChildWithInheritedField(ParentWithInheritedField):
    pass


class SiblingWithInheritedField(ParentWithInheritedField):
    pass


def test__router_generation__child_had_inherited_field__should_not_change_parent_and_siblings(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/parent", response_model=ParentWithInheritedField)
    async def parent(payload: ParentWithInheritedField):
        return payload

    @router.post("/child", response_model=ChildWithInheritedField)
    async def child(payload: ChildWithInheritedField):
        return payload

    @router.post("/sibling", response_model=SiblingWithInheritedField)
    async def sibling(payload: SiblingWithInheritedField):
        return payload

    clients = create_versioned_clients(
        version_change(schema(ParentWithInheritedField).field("foo").had(description="Older description")),
        version_change(
            schema(ParentWithInheritedField).field("bar").had(description="Bar"),
            schema(ChildWithInheritedField).field("foo").had(description="Child description", max_length=3),
        ),
        router=router,
    )

    # The changes of the parent's field in even older versions still apply to the child
    for version, path, schema_name, description, max_length in [
        ("2001-01-01", "/parent", "ParentWithInheritedField", "Head description", 10),
        ("2001-01-01", "/child", "ChildWithInheritedField", "Child description", 3),
        ("2001-01-01", "/sibling", "SiblingWithInheritedField", "Head description", 10),
        ("2000-01-01", "/parent", "ParentWithInheritedField", "Older description", 10),
        ("2000-01-01", "/child", "ChildWithInheritedField", "Older description", 3),
        ("2000-01-01", "/sibling", "SiblingWithInheritedField", "Older description", 10),
    ]:
        client = clients[version]
        assert client.post(path, json={"foo": "a" * 3}).status_code == 200
        assert client.post(path, json={"foo": "a" * 4}).status_code == (200 if max_length == 10 else 422)
        openapi = client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"][schema_name]["properties"]["foo"]
        assert field_schema["description"] == description
        assert field_schema["maxLength"] == max_length


class RecordWithInheritedCode(BaseModel):
    code: str
    quantity: int


class BusinessRecordWithInheritedCode(RecordWithInheritedCode):
    pass


class PersonalRecordWithInheritedCode(RecordWithInheritedCode):
    pass


@pytest.mark.parametrize("in_head_version", [False, True])
def test__router_generation__child_had_serialization_alias_of_inherited_field__should_not_change_keys_of_sibling(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    in_head_version: bool,
):
    @router.get("/record", response_model=RecordWithInheritedCode)
    async def get_record():
        return RecordWithInheritedCode(code="ABC", quantity=1)

    @router.get("/business", response_model=BusinessRecordWithInheritedCode)
    async def get_business():
        return BusinessRecordWithInheritedCode(code="ABC", quantity=1)

    @router.get("/personal", response_model=PersonalRecordWithInheritedCode)
    async def get_personal():
        return PersonalRecordWithInheritedCode(code="ABC", quantity=1)

    change = version_change(
        schema(RecordWithInheritedCode).field("quantity").had(description="Units"),
        schema(BusinessRecordWithInheritedCode).field("code").had(serialization_alias="legacy_code"),
    )
    if in_head_version:
        clients = create_versioned_clients(head_version_changes=[change], router=router)
    else:
        clients = create_versioned_clients(change, router=router)

    # The parent has an instruction of its own (for "quantity"), so Cadwyn 7.4.0 also changed the keys of the parent
    # and of the personal record in the older versions (or, with HeadVersion, in every version) (#466)
    versions_with_alias = ["2000-01-01", "2001-01-01"] if in_head_version else ["2000-01-01"]
    for version, versioned_client in clients.items():
        assert versioned_client.get("/record").json() == {"code": "ABC", "quantity": 1}
        assert versioned_client.get("/personal").json() == {"code": "ABC", "quantity": 1}
        business_key = "legacy_code" if version in versions_with_alias else "code"
        assert versioned_client.get("/business").json() == {business_key: "ABC", "quantity": 1}


class DeletableAttributesInFieldDefaults(BaseModel):
    min_length_field: str = Field(min_length=3)
    max_length_field: str = Field(max_length=3)
    ge_field: int = Field(ge=5)
    le_field: int = Field(le=5)
    pattern_field: str = Field(pattern="^a")
    description_field: str = Field(default="", description="Custom description")
    title_field: str = Field(default="", title="Custom title")
    validated_title_field: Annotated[str, AfterValidator(str.strip)] = Field(default="", title="Custom title")


class DeletableAttributesInAnnotated(BaseModel):
    min_length_field: Annotated[str, Field(min_length=3)]
    max_length_field: Annotated[str, Field(max_length=3)]
    ge_field: Annotated[int, Field(ge=5)]
    le_field: Annotated[int, Field(le=5)]
    pattern_field: Annotated[str, Field(pattern="^a")]
    description_field: Annotated[str, Field(description="Custom description")] = ""
    title_field: Annotated[str, Field(title="Custom title")] = ""
    validated_title_field: Annotated[str, AfterValidator(str.strip), Field(title="Custom title")] = ""


PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS = {
    "min_length_field": "abc",
    "max_length_field": "abc",
    "ge_field": 5,
    "le_field": 5,
    "pattern_field": "abc",
}


@pytest.fixture
def deletable_attributes_router(router: VersionedAPIRouter) -> VersionedAPIRouter:
    @router.post("/field_defaults")
    async def field_defaults(payload: DeletableAttributesInFieldDefaults):
        return payload.model_dump()

    @router.post("/annotated")
    async def annotated(payload: DeletableAttributesInAnnotated):
        return payload.model_dump()

    return router


@pytest.mark.parametrize(
    ("path", "schema_name", "is_removed"),
    [
        ("/field_defaults", "DeletableAttributesInFieldDefaults", True),
        # Cadwyn 7.4.0 ignored didnt_have() for the constraints of a Field() in the annotation's metadata. Removing
        # them could make the older versions reject or change the data that they have always accepted or returned
        # (e.g. through `ConfigDict(str_max_length=...)` or a `left_to_right` union), so it stays ignored
        ("/annotated", "DeletableAttributesInAnnotated", False),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "attribute", "json_schema_key", "value_valid_only_in_old_version"),
    [
        ("min_length_field", "min_length", "minLength", "ab"),
        ("max_length_field", "max_length", "maxLength", "abcdef"),
        ("ge_field", "ge", "minimum", 1),
        ("le_field", "le", "maximum", 50),
        ("pattern_field", "pattern", "pattern", "bc"),
    ],
)
def test__router_generation__field_didnt_have_constraint__should_remove_it_unless_field_in_annotation_sets_it(
    deletable_attributes_router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    path: str,
    schema_name: str,
    is_removed: bool,
    field_name: str,
    attribute: Any,
    json_schema_key: str,
    value_valid_only_in_old_version: object,
):
    @convert_request_to_next_version_for(DeletableAttributesInFieldDefaults, DeletableAttributesInAnnotated)
    def make_request_valid_in_head_version(request: RequestInfo):
        request.body.update(PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS)

    clients = create_versioned_clients(
        version_change(
            schema(DeletableAttributesInFieldDefaults).field(field_name).didnt_have(attribute),
            schema(DeletableAttributesInAnnotated).field(field_name).didnt_have(attribute),
            make_request_valid_in_head_version=make_request_valid_in_head_version,
        ),
        router=deletable_attributes_router,
    )
    payload = PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS | {field_name: value_valid_only_in_old_version}

    for version, expected_status_code in [("2001-01-01", 422), ("2000-01-01", 200 if is_removed else 422)]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"][schema_name]["properties"][field_name]
        assert (json_schema_key in field_schema) == (version == "2001-01-01" or not is_removed)

        assert client.post(path, json=PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS).status_code == 200
        assert client.post(path, json=payload).status_code == expected_status_code


@pytest.mark.parametrize(
    ("path", "schema_name"),
    [
        ("/field_defaults", "DeletableAttributesInFieldDefaults"),
        ("/annotated", "DeletableAttributesInAnnotated"),
    ],
)
def test__router_generation__field_didnt_have_description_and_title__should_remove_them(
    deletable_attributes_router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    path: str,
    schema_name: str,
):
    clients = create_versioned_clients(
        version_change(
            schema(DeletableAttributesInFieldDefaults).field("description_field").didnt_have("description"),
            schema(DeletableAttributesInFieldDefaults).field("title_field").didnt_have("title"),
            schema(DeletableAttributesInFieldDefaults).field("validated_title_field").didnt_have("title"),
            schema(DeletableAttributesInAnnotated).field("description_field").didnt_have("description"),
            schema(DeletableAttributesInAnnotated).field("title_field").didnt_have("title"),
            schema(DeletableAttributesInAnnotated).field("validated_title_field").didnt_have("title"),
        ),
        router=deletable_attributes_router,
    )

    for version, description, title, validated_title in [
        ("2001-01-01", "Custom description", "Custom title", "Custom title"),
        ("2000-01-01", None, "Title Field", "Validated Title Field"),
    ]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        properties = openapi["components"]["schemas"][schema_name]["properties"]
        assert properties["description_field"].get("description") == description
        assert properties["title_field"]["title"] == title
        assert properties["validated_title_field"]["title"] == validated_title
        assert client.post(path, json=PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS).status_code == 200


class SchemaWithUnsetAttributesInAnnotated(BaseModel):
    conint_field: Annotated[conint(lt=7), Field(description="Description")]  # ty: ignore[invalid-type-form]
    min_length_field: Annotated[str, Field(min_length=1)]


@pytest.mark.parametrize(("field_name", "attribute"), [("conint_field", "gt"), ("min_length_field", "description")])
def test__router_generation__field_didnt_have_attribute_that_annotation_metadata_leaves_unset__should_change_nothing(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    field_name: str,
    attribute: Any,
):
    @router.post("/test")
    async def route(payload: SchemaWithUnsetAttributesInAnnotated):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithUnsetAttributesInAnnotated).field(field_name).didnt_have(attribute)),
    )

    openapi_schemas = [
        versioned_client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        for version, versioned_client in clients.items()
    ]
    assert openapi_schemas[0] == openapi_schemas[1]
    for versioned_client in clients.values():
        assert versioned_client.post("/test", json={"conint_field": 6, "min_length_field": "a"}).status_code == 200
        assert versioned_client.post("/test", json={"conint_field": 7, "min_length_field": "a"}).status_code == 422
        assert versioned_client.post("/test", json={"conint_field": 6, "min_length_field": ""}).status_code == 422


class ParentWithClassVar(BaseModel):
    value: ClassVar[str] = "inherited"


with pytest.warns(UserWarning, match='Field name "value" .* shadows an attribute in parent'):

    class ChildRedeclaringClassVarAsField(ParentWithClassVar):
        value: Annotated[str, Field(min_length=2)] = Field()  # ty: ignore[invalid-attribute-override]


@pytest.mark.filterwarnings('ignore:Field name "value" .* shadows an attribute in parent:UserWarning')
def test__router_generation__field_redeclaring_parent_class_var__should_stay_required(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/test")
    async def route(payload: ChildRedeclaringClassVarAsField):
        return payload.model_dump()

    clients = create_versioned_clients(version_change())

    for version, versioned_client in clients.items():
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["ChildRedeclaringClassVarAsField"]["required"] == ["value"]
        assert versioned_client.post("/test", json={}).status_code == 422
        assert versioned_client.post("/test", json={"value": "ab"}).json() == {"value": "ab"}


class Trimmed(StringConstraints):
    def __init__(self, min_length: int = 2) -> None:
        super().__init__(min_length=min_length, strip_whitespace=True)


class SchemaWithConstraintsSubclass(BaseModel):
    foo: Annotated[str, Trimmed()]


def test__router_generation__field_didnt_have_constraint_of_constraints_subclass_with_custom_init__should_remove_it(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/test")
    async def route(payload: SchemaWithConstraintsSubclass):
        return payload.model_dump()

    @convert_request_to_next_version_for(SchemaWithConstraintsSubclass)
    def pad_foo(request: RequestInfo):
        request.body["foo"] = request.body["foo"].ljust(2, "_")

    clients = create_versioned_clients(
        version_change(schema(SchemaWithConstraintsSubclass).field("foo").didnt_have("min_length"), pad_foo=pad_foo),
    )

    assert clients["2001-01-01"].post("/test", json={"foo": " a "}).status_code == 422
    assert clients["2001-01-01"].post("/test", json={"foo": " ab "}).json() == {"foo": "ab"}
    assert clients["2000-01-01"].post("/test", json={"foo": " a "}).json() == {"foo": "a_"}


FIELD_INFO_WITH_LEN = FieldInfo.from_annotation(Annotated[str, Len(2, 5)])  # ty: ignore[invalid-argument-type]


class SchemaWithLenConstraints(BaseModel):
    annotated_len: Annotated[str, Len(2, 5)]
    field_info_with_len: Annotated[str, FIELD_INFO_WITH_LEN]


@pytest.mark.parametrize(
    ("field_name", "old_min_length", "old_status_code_of_too_short_value"),
    [
        ("annotated_len", None, 200),
        # Cadwyn 7.4.0 ignored didnt_have() for the constraints of a Field() in the annotation's metadata. Removing
        # them could make the older versions reject or change the data that they have always accepted or returned,
        # so it stays ignored
        ("field_info_with_len", 2, 422),
    ],
)
def test__router_generation__field_didnt_have_min_length_of_len__should_keep_max_length(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    field_name: str,
    old_min_length: Union[int, None],
    old_status_code_of_too_short_value: int,
):
    @router.post("/test")
    async def route(payload: SchemaWithLenConstraints):
        return payload.model_dump()

    @convert_request_to_next_version_for(SchemaWithLenConstraints)
    def pad_values(request: RequestInfo):
        request.body[field_name] = request.body[field_name].ljust(2, "_")

    clients = create_versioned_clients(
        version_change(
            schema(SchemaWithLenConstraints).field(field_name).didnt_have("min_length"), pad_values=pad_values
        ),
    )
    payload = {"annotated_len": "abc", "field_info_with_len": "abc"}

    for version, min_length, status_code_of_too_short_value in [
        ("2001-01-01", 2, 422),
        ("2000-01-01", old_min_length, old_status_code_of_too_short_value),
    ]:
        versioned_client = clients[version]
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"]["SchemaWithLenConstraints"]["properties"][field_name]
        assert field_schema.get("minLength") == min_length
        assert field_schema["maxLength"] == 5
        assert versioned_client.post("/test", json=payload | {field_name: "a"}).status_code == (
            status_code_of_too_short_value
        )
        assert versioned_client.post("/test", json=payload | {field_name: "abcdef"}).status_code == 422


@dataclasses.dataclass(frozen=True)
class NameConstraints(StringConstraints):
    min_length: int = 2


@dataclasses.dataclass(frozen=True)
class LenWithMinimum(Len):
    min_length: int = 2


class SchemaWithGroupedConstraintSubclasses(BaseModel):
    string_constraints_subclass: Annotated[str, NameConstraints()]
    len_subclass: Annotated[str, LenWithMinimum(max_length=5)]


@pytest.mark.parametrize("field_name", ["string_constraints_subclass", "len_subclass"])
def test__router_generation__field_didnt_have_constraint_with_default_in_grouped_constraint_subclass__should_remove_it(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    field_name: str,
):
    @router.post("/test")
    async def route(payload: SchemaWithGroupedConstraintSubclasses):
        return payload.model_dump()

    @convert_request_to_next_version_for(SchemaWithGroupedConstraintSubclasses)
    def pad_values(request: RequestInfo):
        request.body[field_name] = request.body[field_name].ljust(2, "_")

    clients = create_versioned_clients(
        version_change(
            schema(SchemaWithGroupedConstraintSubclasses).field(field_name).didnt_have("min_length"),
            pad_values=pad_values,
        ),
    )
    payload = {"string_constraints_subclass": "abc", "len_subclass": "abc"}

    for version, min_length, status_code_of_empty_value in [("2001-01-01", 2, 422), ("2000-01-01", None, 200)]:
        versioned_client = clients[version]
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        properties = openapi["components"]["schemas"]["SchemaWithGroupedConstraintSubclasses"]["properties"]
        assert properties[field_name].get("minLength") == min_length
        assert versioned_client.post("/test", json=payload | {field_name: ""}).status_code == (
            status_code_of_empty_value
        )


class SchemaWithSameConstraintBeforeAndAfterValidator(BaseModel):
    value: Annotated[str, AfterValidator(lambda v: v * 3), MinLen(3)] = Field(min_length=3)


def test__router_generation__same_constraint_before_and_after_validator__should_be_checked_on_both_sides(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/test")
    async def route(payload: SchemaWithSameConstraintBeforeAndAfterValidator):
        return payload.model_dump()

    clients = create_versioned_clients(version_change())

    for versioned_client in clients.values():
        assert versioned_client.post("/test", json={"value": "a"}).status_code == 422
        assert versioned_client.post("/test", json={"value": "abc"}).status_code == 200


class SchemaWithConstraintClasses(BaseModel):
    strict_field: Annotated[int, Strict]
    allow_inf_nan_field: Annotated[float, AllowInfNan]


@pytest.mark.parametrize(
    ("field_name", "instruction", "value", "head_status_code", "old_status_code"),
    [
        # Cadwyn 7.4.0 ignored had() for constraint classes, like for other constraints in the annotation's metadata.
        # Applying it could make the older versions reject the requests that they have always accepted (as in
        # had(allow_inf_nan=False)), so it stays ignored. didnt_have() failed with TypeError there instead
        ("strict_field", lambda field: field.had(strict=False), "1", 422, 422),
        ("strict_field", lambda field: field.didnt_have("strict"), "1", 422, 200),
        ("allow_inf_nan_field", lambda field: field.had(allow_inf_nan=False), "inf", 200, 200),
        ("allow_inf_nan_field", lambda field: field.didnt_have("allow_inf_nan"), "inf", 200, 200),
    ],
)
def test__router_generation__field_had_and_didnt_have__constraint_class_used_as_metadata(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    field_name: str,
    instruction: Callable[[Any], Any],
    value: object,
    head_status_code: int,
    old_status_code: int,
):
    @router.post("/test")
    async def route(payload: SchemaWithConstraintClasses):
        return {"ok": True}

    clients = create_versioned_clients(
        version_change(instruction(schema(SchemaWithConstraintClasses).field(field_name)))
    )
    payload = {"strict_field": 1, "allow_inf_nan_field": 1.0, field_name: value}

    assert clients["2001-01-01"].post("/test", json=payload).status_code == head_status_code
    assert clients["2000-01-01"].post("/test", json=payload).status_code == old_status_code


class SchemaWithDocStringInAnnotated(BaseModel):
    foo: Annotated[str, "Some documentation", Field(title="Custom title")]


def test__router_generation__field_didnt_have_title__with_string_in_annotated__should_remove_it(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/test")
    async def route(payload: SchemaWithDocStringInAnnotated):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithDocStringInAnnotated).field("foo").didnt_have("title")),
    )

    for version, title in [("2001-01-01", "Custom title"), ("2000-01-01", "Foo")]:
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["SchemaWithDocStringInAnnotated"]["properties"]["foo"]["title"] == title
        assert clients[version].post("/test", json={"foo": "bar"}).json() == {"foo": "bar"}


class SchemaWithJsonSchemaExtraInAnnotated(BaseModel):
    foo: Annotated[str, Field(json_schema_extra={"a": 1, "b": 2})]


def test__router_generation__field_had_json_schema_extra__with_field_info_in_annotated__should_merge_them(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/test")
    async def route(payload: SchemaWithJsonSchemaExtraInAnnotated):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithJsonSchemaExtraInAnnotated).field("foo").had(json_schema_extra={"a": 0})),
    )

    for version, expected_extra in [("2001-01-01", {"a": 1, "b": 2}), ("2000-01-01", {"a": 0, "b": 2})]:
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"]["SchemaWithJsonSchemaExtraInAnnotated"]["properties"]["foo"]
        assert {key: field_schema[key] for key in ("a", "b") if key in field_schema} == expected_extra
        assert clients[version].post("/test", json={"foo": "bar"}).json() == {"foo": "bar"}


class AnnotatedCat(BaseModel):
    kind: Literal["cat"] = "cat"


class AnnotatedDog(BaseModel):
    kind: Literal["dog"] = "dog"


class SchemaWithWireAttributesInAnnotated(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=10)]
    with_default: Annotated[int, Field(default=1)]
    with_default_factory: Annotated[list[int], Field(default_factory=list)]
    with_validation_alias: Annotated[str, Field(validation_alias="validationAlias")] = ""
    with_serialization_alias: Annotated[str, Field(serialization_alias="serializationAlias")] = ""
    with_exclude: Annotated[str, Field(exclude=True)] = "hidden"
    with_coerce_numbers_to_str: Annotated[str, Field(coerce_numbers_to_str=True)] = ""
    with_discriminator: Annotated[Union[AnnotatedCat, AnnotatedDog], Field(discriminator="kind")] = AnnotatedCat()


class StrictSchemaWithLaxFieldInAnnotated(BaseModel):
    model_config = ConfigDict(strict=True)

    not_strict: Annotated[int, Field(strict=False)] = 0


@pytest.mark.parametrize(
    ("model", "attribute", "instruction"),
    [
        *(
            pytest.param(
                SchemaWithWireAttributesInAnnotated,
                "name",
                instruction,
                marks=pytest.mark.skipif(
                    HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED,
                    reason="Before pydantic 2.12, had() changes such constraints, as in Cadwyn 7.4.0",
                ),
            )
            for instruction in [lambda field: field.had(min_length=5), lambda field: field.had(max_length=3)]
        ),
        (SchemaWithWireAttributesInAnnotated, "name", lambda field: field.didnt_have("min_length")),
        (SchemaWithWireAttributesInAnnotated, "name", lambda field: field.didnt_have("max_length")),
        (SchemaWithWireAttributesInAnnotated, "with_default", lambda field: field.didnt_have("default")),
        (SchemaWithWireAttributesInAnnotated, "with_default_factory", lambda f: f.didnt_have("default_factory")),
        (SchemaWithWireAttributesInAnnotated, "with_validation_alias", lambda f: f.didnt_have("validation_alias")),
        (
            SchemaWithWireAttributesInAnnotated,
            "with_serialization_alias",
            lambda field: field.didnt_have("serialization_alias"),
        ),
        (SchemaWithWireAttributesInAnnotated, "with_exclude", lambda field: field.didnt_have("exclude")),
        (
            SchemaWithWireAttributesInAnnotated,
            "with_coerce_numbers_to_str",
            lambda field: field.didnt_have("coerce_numbers_to_str"),
        ),
        (SchemaWithWireAttributesInAnnotated, "with_discriminator", lambda f: f.didnt_have("discriminator")),
        (StrictSchemaWithLaxFieldInAnnotated, "not_strict", lambda field: field.didnt_have("strict")),
    ],
)
def test__router_generation__field_had_and_didnt_have__attributes_of_field_info_in_annotated__should_keep_wire_format(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    model: type[BaseModel],
    attribute: str,
    instruction: Callable[[Any], Any],
):
    """Cadwyn 7.4.0 ignored these instructions for the attributes of a Field() in the annotation's metadata.

    Applying them would make the older versions reject or change the requests and responses that they have always
    accepted or returned, so they stay ignored and the older versions behave exactly like the head version.
    """

    @router.post("/test", response_model=model)
    async def post(payload: model):  # ty: ignore[invalid-type-form]
        return payload

    @router.get("/test", response_model=model)
    async def get():
        if model is StrictSchemaWithLaxFieldInAnnotated:
            return StrictSchemaWithLaxFieldInAnnotated()
        return SchemaWithWireAttributesInAnnotated(name="abcdefgh", with_exclude="secret")

    clients = create_versioned_clients(version_change(instruction(schema(model).field(attribute))))
    payloads = [
        {},
        {"not_strict": "4"},
        {"name": "ab"},
        {"name": "abcdefgh", "with_default": 2, "with_default_factory": [3]},
        {"name": "ab", "validationAlias": "validated", "with_validation_alias": "unused"},
        {"name": "ab", "with_serialization_alias": "serialized", "with_exclude": "secret"},
        {"name": "ab", "with_coerce_numbers_to_str": 5},
        {"name": "ab", "with_discriminator": {"kind": "dog"}},
        {"name": "ab", "with_discriminator": {}},
    ]

    def responses(version: str) -> list[object]:
        client = clients[version]
        return [
            *[(r.status_code, r.json()) for r in (client.post("/test", json=payload) for payload in payloads)],
            client.get("/test").json(),
            client.get(f"/openapi.json?version={version}").json()["components"]["schemas"],
        ]

    assert responses("2000-01-01") == responses("2001-01-01")
    if model is SchemaWithWireAttributesInAnnotated:
        assert clients["2000-01-01"].get("/test").json() == {
            "name": "abcdefgh",
            "with_default": 1,
            "with_default_factory": [],
            "with_validation_alias": "",
            "serializationAlias": "",
            "with_coerce_numbers_to_str": "",
            "with_discriminator": {"kind": "cat"},
        }


class SchemaWithOnlyDocStringInAnnotated(BaseModel):
    foo: Annotated[str, "Some documentation"]


def test__router_generation__field_didnt_have_unset_title__with_string_in_annotated__should_raise_error(
    create_versioned_clients: CreateVersionedClients,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match='You tried to delete the attribute "title" of field "foo" from "SchemaWithOnlyDocStringInAnnotated"',
    ):
        generate_versioned_models(
            VersionBundle(
                Version(
                    "2001-01-01",
                    version_change(schema(SchemaWithOnlyDocStringInAnnotated).field("foo").didnt_have("title")),
                ),
                Version("2000-01-01"),
            )
        )


class SchemaWithConstrainedAnnotatedField(BaseModel):
    foo: Annotated[str, Field(min_length=2)]


def test__router_generation__field_didnt_have_default_that_annotation_metadata_leaves_unset__should_stay_required(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/test")
    async def route(payload: SchemaWithConstrainedAnnotatedField):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithConstrainedAnnotatedField).field("foo").didnt_have("default")),
    )

    for version, versioned_client in clients.items():
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["SchemaWithConstrainedAnnotatedField"]["required"] == ["foo"]
        assert versioned_client.post("/test", json={}).status_code == 422
        assert versioned_client.post("/test", json={"foo": "a"}).status_code == 422
        assert versioned_client.post("/test", json={"foo": "ab"}).json() == {"foo": "ab"}


def test__router_generation__updating_request_depends(
    router: VersionedAPIRouter, create_versioned_app: CreateVersionedApp
):
    def sub_dependency1(my_schema: EmptySchema) -> EmptySchema:
        return my_schema

    def dependency1(dep: EmptySchema = Depends(sub_dependency1)):
        return dep

    def sub_dependency2(my_schema: EmptySchema) -> EmptySchema:
        return my_schema

    # TASK: What if "a" gets deleted? https://github.com/zmievsa/cadwyn/issues/25
    def dependency2(
        dep: Annotated[Union[EmptySchema, None], Depends(sub_dependency2)] = None,
    ):
        return dep

    @router.post("/test1")
    async def test_with_dep1(dep: EmptySchema = Depends(dependency1)):
        return dep

    @router.post("/test2")
    async def test_with_dep2(dep: EmptySchema = Depends(dependency2)):
        return dep

    app = create_versioned_app(version_change(schema(EmptySchema).field("foo").existed_as(type=str)))

    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    resp_from_test1 = client_2000.post("/test1", json={}).json()
    resp_from_test2 = client_2000.post("/test2", json={}).json()
    assert resp_from_test1 == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "foo"],
                "msg": "Field required",
                "input": {},
            },
        ],
    }
    assert resp_from_test2 == {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "foo"],
                "msg": "Field required",
                "input": {},
            },
        ],
    }

    assert client_2000.post("/test1", json={"foo": "bar"}).json() == {}
    assert client_2000.post("/test2", json={"foo": "bar"}).json() == {}

    assert client_2001.post("/test1", json={}).json() == {}
    assert client_2001.post("/test1", json={"my_schema": {"foo": "bar"}}).json() == {}

    assert client_2001.post("/test2", json={}).json() == {}
    assert client_2001.post("/test2", json={"my_schema": {"foo": "bar"}}).json() == {}


def test__router_generation__enum_member_dependency_default__fails_without_member_only_if_fastapi_validates_defaults(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    def dependency(value: StrEnum = StrEnum.a) -> StrEnum:
        return value

    @router.get("/test")
    async def route(value: StrEnum = Depends(dependency)):
        return value.name

    clients = create_versioned_clients(
        version_change(enum(StrEnum).had(a=StrEnum.a.value)),
        version_change(enum(StrEnum).didnt_have("a")),
    )

    assert clients["2002-01-01"].get("/test").json() == "a"
    response = clients["2001-01-01"].get("/test")
    # This version's enum has no such member, so the default fails only where FastAPI validates it
    rejected_default = {
        "detail": [
            {
                "type": "is_instance_of",
                "loc": ["query", "value"],
                "msg": "Input should be an instance of StrEnum",
                "input": StrEnum.a.value,
                "ctx": {"class": "StrEnum"},
            }
        ]
    }
    expected = (422, rejected_default) if FASTAPI_VALIDATES_DEFAULTS_OF_OMITTED_PARAMS else (200, "a")
    assert (response.status_code, response.json()) == expected
    assert clients["2000-01-01"].get("/test").json() == "a"


def test__router_generation__using_unversioned_schema_in_body(
    router: VersionedAPIRouter, create_versioned_app: CreateVersionedApp
):
    class MySchema(BaseModel):
        bar: str

    @router.post("/test")
    async def test_with_dep1(dep: MySchema):
        return dep

    app = create_versioned_app(version_change())

    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test", json={"bar": "hello"}).json() == {"bar": "hello"}
    assert client_2001.post("/test", json={"bar": "hello"}).json() == {"bar": "hello"}


def test__router_generation_updating_unused_dependencies__with_migration(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    saved_enum_names = []

    async def dependency(my_enum: StrEnum):
        saved_enum_names.append(my_enum.name)

    @router.get("/test", dependencies=[Depends(dependency)])
    async def test_with_dep():
        pass

    def migration(request: Any):
        return None

    app = create_versioned_app(
        version_change(
            enum(StrEnum).didnt_have("a"),
            enum(StrEnum).had(b="1"),
            migration=convert_request_to_next_version_for("/test", ["GET"])(migration),
        ),
    )

    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    resp = client_2000.get("/test", params={"my_enum": "1"})
    assert resp.status_code == 200

    resp = client_2001.get("/test", params={"my_enum": "1"})
    assert resp.status_code == 200

    assert saved_enum_names == [
        "b",  # FastAPI called our dependency and got b in 2000
        "a",  # We called our dependency and got a in 2001
        "a",  # FastAPI called our dependency and got a in 2001
        "a",  # We called our dependency and got a in 2001
    ]


def test__router_generation__updating_callbacks(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    callback_router = APIRouter()

    @callback_router.websocket_route("/{request}")
    def useless_callback():
        raise NotImplementedError

    @callback_router.get("{request.body}")
    def callback(body: SchemaWithOneIntField):
        raise NotImplementedError

    @router.post("/test", callbacks=callback_router.routes)
    async def test_with_callbacks(body: SchemaWithOneIntField):
        raise NotImplementedError

    app = create_versioned_app(
        version_change(schema(SchemaWithOneIntField).field("bar").existed_as(type=str)),
    )

    route = app.router.versioned_routers["2000-01-01"].routes[1]
    assert isinstance(route, APIRoute)
    assert route.callbacks is not None
    generated_callback = route.callbacks[1]
    assert isinstance(generated_callback, APIRoute)
    body_model = generated_callback.dependant.body_params[0].field_info.annotation
    assert isinstance(body_model, type)
    assert issubclass(body_model, BaseModel)
    assert body_model.model_fields["bar"].annotation is str

    route = app.router.versioned_routers["2001-01-01"].routes[1]
    assert isinstance(route, APIRoute)
    assert route.callbacks is not None
    generated_callback = route.callbacks[1]
    assert isinstance(generated_callback, APIRoute)
    body_model = generated_callback.dependant.body_params[0].field_info.annotation
    assert isinstance(body_model, type)
    assert issubclass(body_model, BaseModel)
    assert "bar" not in body_model.model_fields


def test__cascading_router_exists(router: VersionedAPIRouter, api_version_var: ContextVar[Union[str, None]]):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_with_dep1():
        return 83

    class V2002(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).existed]

    versions = VersionBundle(
        Version("2002-01-01", V2002),
        Version("2001-01-01"),
        Version("2000-01-01"),
        api_version_var=api_version_var,
    )
    routers = generate_versioned_routers(router, versions=versions)

    assert client(routers.endpoints["2002-01-01"]).get("/test").json() == {"detail": "Not Found"}
    assert client(routers.endpoints["2001-01-01"]).get("/test").json() == 83
    assert client(routers.endpoints["2000-01-01"]).get("/test").json() == 83


def test__cascading_router_didnt_exist(
    router: VersionedAPIRouter,
    api_version_var: ContextVar[Union[str, None]],
):
    @router.get("/test")
    async def test_with_dep1():
        return 83

    class V2002(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [
            endpoint("/test", ["GET"]).didnt_exist,
        ]

    versions = VersionBundle(
        Version("2002-01-01", V2002),
        Version("2001-01-01"),
        Version("2000-01-01"),
        api_version_var=api_version_var,
    )
    routers = generate_versioned_routers(router, versions=versions)

    assert client(routers.endpoints["2002-01-01"]).get("/test").json() == 83

    assert client(routers.endpoints["2001-01-01"]).get("/test").json() == {
        "detail": "Not Found",
    }

    assert client(routers.endpoints["2000-01-01"]).get("/test").json() == {
        "detail": "Not Found",
    }


def test__generate_versioned_routers__two_routers(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
    api_version_var: ContextVar[Union[str, None]],
):
    router2 = VersionedAPIRouter(prefix="/api2")

    @router2.get("/test")
    async def test_endpoint2():
        raise NotImplementedError

    class V2001(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [
            endpoint(test_path, ["GET"]).didnt_exist,
            endpoint("/api2/test", ["GET"]).had(description="Meaw"),
        ]

    versions = VersionBundle(
        Version("2001-01-01", V2001),
        Version("2000-01-01"),
        api_version_var=api_version_var,
    )

    root_router = APIRouter()
    root_router.include_router(router)
    root_router.include_router(router2)

    routers = generate_versioned_routers(root_router, versions=versions).endpoints
    assert all(type(r) is APIRouter for r in routers.values())
    assert len(routers["2001-01-01"].routes) == 2
    assert len(routers["2000-01-01"].routes) == 1
    assert {
        get_wrapped_endpoint(route_endpoint(routers["2001-01-01"].routes[0])),
        get_wrapped_endpoint(route_endpoint(routers["2001-01-01"].routes[1])),
    } == {
        test_endpoint,
        test_endpoint2,
    }
    assert endpoints_equal(route_endpoint(routers["2000-01-01"].routes[0]), test_endpoint2)
    assert endpoints_equal(route_endpoint(routers["2000-01-01"].routes[0]), test_endpoint2)


class MyHTTPBearer(HTTPBearer):
    pass


@pytest.mark.parametrize(
    ("security_cls", "expected_status_code"),
    [(HTTPBearer, 401), (MyHTTPBearer, 401), (HTTPBasic, 401)],
)
def test__basic_router_generation__using_http_security_dependency__should_generate_the_required_security_params(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    security_cls: Union[type[HTTPBearer], type[MyHTTPBearer], type[HTTPBasic]],
    expected_status_code: int,
):
    auth_header_scheme = security_cls(description="Bearer token for authentication")

    def auth(
        auth_header: Annotated[
            Union[HTTPAuthorizationCredentials, None],
            Depends(auth_header_scheme),
        ],
    ):
        raise NotImplementedError

    @router.get("/test", dependencies=[Depends(auth)])
    async def test():
        raise NotImplementedError

    client_2000, *_ = create_versioned_clients().values()

    dependant = cast("APIRoute", client_2000.app.router.versioned_routers["2000-01-01"].routes[-1]).dependant
    assert dependant.dependencies[1].dependencies[0].call is auth_header_scheme
    response = client_2000.get("/test")
    assert response.status_code == expected_status_code
    assert response.json() == {"detail": "Not authenticated"}


def test__basic_router_generation__using_custom_class_based_dependency__should_migrate_as_usual(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    payloads_dependency_was_called_with = []

    class MyCustomDependency:
        def __call__(self, my_body: SchemaWithOneIntField):
            payloads_dependency_was_called_with.append(my_body.model_dump())
            return my_body

    @router.post("/test")
    async def route(dependency: Any = Depends(MyCustomDependency())):
        return dependency

    client_2000, client_2001 = create_versioned_clients(
        version_change(schema(SchemaWithOneIntField).field("bar").existed_as(type=str)),
    ).values()

    response = client_2000.post("/test", json={"foo": 3, "bar": "meaw"})
    assert response.status_code == 200
    assert response.json() == {"foo": 3}

    response = client_2001.post("/test", json={"foo": 3})
    assert response.status_code == 200
    assert response.json() == {"foo": 3}

    # This is not a nice behavior but this is the way Cadwyn functions: the dependency is going to be called
    # twice: once by FastAPI with solve_dependencies for the old version and once by Cadwyn
    # with solve_dependencies for the new version.
    assert payloads_dependency_was_called_with == [
        {"foo": 3, "bar": "meaw"},  # client_2000
        {"foo": 3},  # client_2000
        {"foo": 3},  # client_2001
        {"foo": 3},  # client_2001
    ]


def test__basic_router_generation__subclass_of_security_class_based_dependency_with_overridden_call__will_not_migrate(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    payloads_dependency_was_called_with = []

    def custom_dependency_call(self: HTTPBearer, my_body: SchemaWithOneIntField):
        payloads_dependency_was_called_with.append(my_body.model_dump())
        return my_body

    my_custom_dependency = type("MyCustomDependency", (HTTPBearer,), {"__call__": custom_dependency_call})

    @router.post("/test")
    async def route(dependency: Any = Depends(my_custom_dependency())):
        return dependency

    client_2000, client_2001 = create_versioned_clients(
        version_change(schema(SchemaWithOneIntField).field("bar").existed_as(type=str)),
    ).values()

    response = client_2000.post("/test", json={"foo": 3, "bar": "meaw"})
    assert response.status_code == 200
    assert response.json() == {"foo": 3}

    response = client_2001.post("/test", json={"foo": 3})
    assert response.status_code == 200
    assert response.json() == {"foo": 3}

    # I.e. It was not migrated at all because we do not migrate any security classes that belong to FastAPI
    assert payloads_dependency_was_called_with == [
        {"foo": 3},  # client_2000
        {"foo": 3},  # client_2000
        {"foo": 3},  # client_2001
        {"foo": 3},  # client_2001
    ]


def test__router_generation__with_generator_dependencies(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    dependency_cache = []

    async def my_async_dependency(current_dependency_runner: Annotated[str, Depends(current_dependency_solver)]):
        dependency_cache.append(f"{current_dependency_runner} async dependency start")
        yield "async dependency"
        dependency_cache.append(f"{current_dependency_runner} async dependency end")

    def my_sync_dependency(current_dependency_runner: Annotated[str, Depends(current_dependency_solver)]):
        dependency_cache.append(f"{current_dependency_runner} sync dependency start")
        yield "sync dependency"
        dependency_cache.append(f"{current_dependency_runner} sync dependency end")

    def route_sync_dependency(my_enum: "StrEnum"):
        dependency_cache.append(f"route sync {my_enum.name} start")
        yield
        dependency_cache.append(f"route sync {my_enum.name} end")

    async def route_async_dependency(my_enum: "StrEnum"):
        dependency_cache.append(f"route async {my_enum.name} start")
        yield
        dependency_cache.append(f"route async {my_enum.name} end")

    @router.get("/test", dependencies=[Depends(route_sync_dependency), Depends(route_async_dependency)])
    async def test(
        my_async_dep: Annotated[str, Depends(my_async_dependency)],
        my_sync_dep: Annotated[str, Depends(my_sync_dependency)],
    ):
        assert my_async_dep == "async dependency"
        assert my_sync_dep == "sync dependency"

    def migration(request: Any):
        return None

    client = TestClient(
        create_versioned_app(
            version_change(
                enum(StrEnum).didnt_have("a"),
                enum(StrEnum).had(b="1"),
                migration=convert_request_to_next_version_for("/test", ["GET"])(migration),
            )
        )
    )
    assert client.get("/test", params={"my_enum": "1"}, headers={"x-api-version": "2000-01-01"}).status_code == 200
    assert dependency_cache == snapshot(
        [
            "route sync b start",
            "route async b start",
            "fastapi async dependency start",
            "fastapi sync dependency start",
            "route sync a start",
            "route async a start",
            "cadwyn async dependency start",
            "cadwyn sync dependency start",
            "cadwyn sync dependency end",
            "cadwyn async dependency end",
            "route async a end",
            "route sync a end",
            "fastapi sync dependency end",
            "fastapi async dependency end",
            "route async b end",
            "route sync b end",
        ]
    )
    dependency_cache.clear()
    assert client.get("/test", params={"my_enum": "1"}, headers={"x-api-version": "2001-01-01"}).status_code == 200
    assert dependency_cache == snapshot(
        [
            "route sync a start",
            "route async a start",
            "fastapi async dependency start",
            "fastapi sync dependency start",
            "route sync a start",
            "route async a start",
            "cadwyn async dependency start",
            "cadwyn sync dependency start",
            "cadwyn sync dependency end",
            "cadwyn async dependency end",
            "route async a end",
            "route sync a end",
            "fastapi sync dependency end",
            "fastapi async dependency end",
            "route async a end",
            "route sync a end",
        ]
    )


def test__router_generation__generator_dependencies__should_receive_exceptions_raised_in_endpoint(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    events = []

    async def async_session(solver: Annotated[str, Depends(current_dependency_solver)]):
        try:
            yield
        except HTTPException:
            events.append(f"{solver} async rollback")
            raise
        finally:
            events.append(f"{solver} async close")

    def sync_session(solver: Annotated[str, Depends(current_dependency_solver)]):
        try:
            yield
        except HTTPException:
            events.append(f"{solver} sync rollback")
            raise
        finally:
            events.append(f"{solver} sync close")

    @router.get("/test")
    async def route(async_value: None = Depends(async_session), sync_value: None = Depends(sync_session)):
        raise HTTPException(status_code=409)

    for version_client in create_versioned_clients(version_change()).values():
        events.clear()
        assert version_client.get("/test").status_code == 409
        assert events == [
            "cadwyn sync rollback",
            "cadwyn sync close",
            "cadwyn async rollback",
            "cadwyn async close",
            "fastapi sync rollback",
            "fastapi sync close",
            "fastapi async rollback",
            "fastapi async close",
        ]


def test__router_generation__async_generator_dependency__should_forward_everything_to_the_original_generator(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    events = []

    async def dependency():
        cleanup_failed = False
        try:
            yield "value"
        except ValueError:
            cleanup_failed = True
        except BaseException as e:
            events.append(f"caught {type(e).__name__}")
            raise
        finally:
            events.append("finally")
        if cleanup_failed:
            raise RuntimeError("cleanup failed")

    # Not in Annotated[...] because such dependencies only get versioned where the version changes their models
    @router.get("/test")
    async def route(value: str = Depends(dependency)):
        raise NotImplementedError

    app = create_versioned_app(version_change())
    versioned_route = cast("APIRoute", app.router.versioned_routers["2000-01-01"].routes[-1])
    # Versioned dependencies are equal to the originals so that dependency overrides work
    (versioned_dependency,) = [dep.call for dep in versioned_route.dependant.dependencies if dep.call == dependency]
    assert versioned_dependency is not dependency

    async def run(call: Callable[[], Any], action: Callable[[Any], Awaitable[Any]]):
        events.clear()
        generator = call()
        assert await generator.__anext__() == "value"
        try:
            await action(generator)
        except BaseException as e:  # noqa: BLE001
            return events.copy(), type(e), None if e.__context__ is None else type(e.__context__)
        return events.copy(), None, None

    expected_results = {
        "aclose": (lambda g: g.aclose(), (["caught GeneratorExit", "finally"], None, None)),
        "asend": (lambda g: g.asend(None), (["finally"], StopAsyncIteration, None)),
        "athrow_handled": (lambda g: g.athrow(ValueError()), (["finally"], RuntimeError, None)),
        "athrow_generator_exit": (
            lambda g: g.athrow(GeneratorExit()),
            (["caught GeneratorExit", "finally"], GeneratorExit, None),
        ),
        "athrow_cancelled": (
            lambda g: g.athrow(asyncio.CancelledError()),
            (["caught CancelledError", "finally"], asyncio.CancelledError, None),
        ),
    }
    for name, (action, expected_result) in expected_results.items():
        assert asyncio.run(run(dependency, action)) == expected_result, name
        assert asyncio.run(run(versioned_dependency, action)) == expected_result, name


def test__router_generation__query_model_defaults__should_stay_isolated_between_versions_and_apps(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    class Filters(BaseModel):
        limit: int = 10

    def get_limit(filters: Filters = Query()) -> int:
        return filters.limit

    class LimitGetter:
        def __call__(self, filters: Filters = Query()) -> int:
            return filters.limit

    @router.get("/endpoint")
    async def endpoint_route(filters: Filters = Query()):
        return filters.limit

    @router.get("/annotated_dependency")
    async def annotated_dependency_route(limit: Annotated[int, Depends(get_limit)]):
        return limit

    @router.get("/default_dependency")
    async def default_dependency_route(limit: int = Depends(get_limit)):
        return limit

    @router.get("/instance_dependency")
    async def instance_dependency_route(limit: Annotated[int, Depends(LimitGetter())]):
        return limit

    paths = ("/endpoint", "/annotated_dependency", "/default_dependency", "/instance_dependency")

    def get_limits(app: Cadwyn) -> dict[tuple[str, str], tuple[int, int]]:
        limits = {}
        with TestClient(app) as client:
            for version in app.router.versioned_routers:
                openapi = client.get(f"/openapi.json?version={version}").json()
                for path in paths:
                    response = client.get(path, headers={"x-api-version": version})
                    parameters = openapi["paths"][path]["get"]["parameters"]
                    (parameter,) = [p for p in parameters if p["name"] == "limit"]
                    limits[version, path] = (response.json(), parameter["schema"]["default"])
        return limits

    app = create_versioned_app(
        version_change(schema(Filters).field("limit").had(default=30)),
        version_change(schema(Filters).field("limit").had(default=20)),
    )
    # The endpoint always gets the head default because Cadwyn migrates requests to head before solving them again
    expected_limits = {
        (version, path): (10, openapi_default)
        for version, openapi_default in {"2002-01-01": 10, "2001-01-01": 20, "2000-01-01": 30}.items()
        for path in paths
    }
    assert get_limits(app) == expected_limits

    create_versioned_app(version_change(schema(Filters).field("limit").had(default=50)))
    assert get_limits(app) == expected_limits


class _QueryWithCustomCopy(fastapi.params.Query):
    def __copy__(self) -> "_QueryWithCustomCopy":
        query_copy = type(self).__new__(type(self))
        query_copy.__dict__.update(self.__dict__)
        for name in FieldInfo.__slots__:
            setattr(query_copy, name, getattr(self, name))
        query_copy.metadata = [Ge(10)]
        return query_copy


def test__router_generation__param_default_with_custom_copy__should_keep_its_own_metadata(
    router: VersionedAPIRouter,
    create_versioned_app: CreateVersionedApp,
):
    """Versions copy the FieldInfo defaults of params but keep the metadata of the original, not of its __copy__"""

    @router.get("/test")
    async def route(x: int = cast("Any", _QueryWithCustomCopy(ge=0))):
        return x

    app = create_versioned_app(version_change())
    with TestClient(app) as client:
        for version in app.router.versioned_routers:
            assert client.get("/test?x=5", headers={"x-api-version": version}).json() == 5
            openapi = client.get(f"/openapi.json?version={version}").json()
            (parameter,) = [p for p in openapi["paths"]["/test"]["get"]["parameters"] if p["name"] == "x"]
            assert parameter["schema"]["minimum"] == 0


def _async_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper


def _async_generator_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        yield await func(*args, **kwargs)

    return wrapper


def _sync_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper


def test__router_generation__decorated_dependencies__should_be_called_the_way_their_outer_decorator_works(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    events = []

    def generator_decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            events.append(f"{kwargs['solver']} enter")
            yield func(*args, **kwargs)
            events.append(f"{kwargs['solver']} exit")

        return wrapper

    @_async_decorator
    def check_token(solver: Annotated[str, Depends(current_dependency_solver)], token: str = "") -> str:
        events.append(f"{solver} token {token}")
        return token

    @generator_decorator
    def get_session(solver: Annotated[str, Depends(current_dependency_solver)]) -> str:
        return "session"

    @_async_generator_decorator
    async def get_value(solver: Annotated[str, Depends(current_dependency_solver)]) -> str:
        return f"value from {solver}"

    def generator_dependency(role: str, solver: Annotated[str, Depends(current_dependency_solver)]) -> Any:
        events.append(f"{solver} {role} enter")
        yield role
        events.append(f"{solver} {role} exit")

    # functools.wraps makes __wrapped__ point to the partial
    decorated_partial = _sync_decorator(functools.partial(generator_dependency, "admin"))

    @router.get("/token")
    async def token_route(token: Annotated[str, Depends(check_token)]):
        return token

    @router.get("/session")
    async def session_route(session: Annotated[str, Depends(get_session)]):
        return session

    @router.get("/value")
    async def value_route(value: Annotated[str, Depends(get_value)]):
        return value

    @router.get("/partial")
    async def partial_route(role: Annotated[str, Depends(decorated_partial)]):
        return role

    for version_client in create_versioned_clients(version_change()).values():
        events.clear()
        assert version_client.get("/token", params={"token": "secret"}).json() == "secret"
        assert events == ["fastapi token secret", "cadwyn token secret"]

        events.clear()
        assert version_client.get("/session").json() == "session"
        assert events == ["fastapi enter", "cadwyn enter", "cadwyn exit", "fastapi exit"]

        assert version_client.get("/value").json() == "value from cadwyn"

        events.clear()
        assert version_client.get("/partial").json() == "admin"
        assert events == ["fastapi admin enter", "cadwyn admin enter", "cadwyn admin exit", "fastapi admin exit"]


def test__router_generation__dependency_with_function_scope__should_exit_before_response_is_sent(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    events = []

    def dependency(solver: Annotated[str, Depends(current_dependency_solver)]):
        yield
        events.append(f"{solver} exit")

    @router.get("/test")
    async def route(value: None = Depends(dependency, scope="function")):
        def stream_events():
            yield json.dumps(events)

        return StreamingResponse(stream_events())

    for version_client in create_versioned_clients(version_change()).values():
        events.clear()
        assert version_client.get("/test").json() == ["cadwyn exit", "fastapi exit"]


def test__router_generation__security_scopes_and_use_cache__should_be_preserved(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    calls = []

    def uncached_dependency(solver: Annotated[str, Depends(current_dependency_solver)]) -> None:
        calls.append(f"{solver} uncached")

    def get_scopes(security_scopes: SecurityScopes, solver: Annotated[str, Depends(current_dependency_solver)]) -> None:
        calls.append(f"{solver} scopes {security_scopes.scopes}")

    @router.get("/test")
    async def route(
        scopes: Annotated[None, Security(get_scopes, scopes=["items:read"])],
        first: Annotated[None, Depends(uncached_dependency, use_cache=False)],
        second: None = Depends(uncached_dependency, use_cache=False),
    ):
        return None

    for version_client in create_versioned_clients(version_change()).values():
        calls.clear()
        assert version_client.get("/test").status_code == 200
        assert calls == [
            "fastapi scopes ['items:read']",
            "fastapi uncached",
            "fastapi uncached",
            "cadwyn scopes ['items:read']",
            "cadwyn uncached",
            "cadwyn uncached",
        ]


def test__router_generation__depends_subclass_with_custom_init__should_be_versioned(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    class TaggedDepends(fastapi.params.Depends):
        def __init__(self, dependency: Callable[..., Any], *, tag: str) -> None:
            super().__init__(dependency)
            object.__setattr__(self, "tag", tag)

    def get_body(body: SchemaWithOneIntField) -> dict[str, Any]:
        return body.model_dump()

    @router.post("/annotated")
    async def annotated_route(value: Annotated[Any, TaggedDepends(get_body, tag="body")]):
        return value

    @router.post("/default")
    async def default_route(value: Any = TaggedDepends(get_body, tag="body")):
        return value

    clients = create_versioned_clients(
        version_change(schema(SchemaWithOneIntField).field("bar").existed_as(type=str)),
    )
    for path in ("/annotated", "/default"):
        assert clients["2000-01-01"].post(path, json={"foo": 1}).status_code == 422
        assert clients["2000-01-01"].post(path, json={"foo": 1, "bar": "baz"}).json() == {"foo": 1}
        assert clients["2001-01-01"].post(path, json={"foo": 1}).json() == {"foo": 1}


class _ResourceWithAsyncCall:
    def __init__(self, times: int) -> None:
        self.times = times

    async def __call__(self) -> None:
        raise NotImplementedError


class _ResourceWithGeneratorCall:
    def __init__(self, times: int) -> None:
        self.times = times

    def __call__(self) -> Any:
        raise NotImplementedError
        yield


class _ResourceWithAsyncGeneratorCall:
    def __init__(self, times: int) -> None:
        self.times = times

    async def __call__(self) -> Any:
        raise NotImplementedError
        yield


@pytest.mark.parametrize(
    "resource_cls", [_ResourceWithAsyncCall, _ResourceWithGeneratorCall, _ResourceWithAsyncGeneratorCall]
)
def test__router_generation__partial_of_class_dependency__should_construct_an_instance(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    resource_cls: type[Any],
):
    @router.get("/annotated")
    async def annotated_route(value: Annotated[Any, Depends(functools.partial(resource_cls, times=3))]):
        return {"type": type(value).__name__, "times": value.times}

    @router.get("/default")
    async def default_route(value: Any = Depends(functools.partial(resource_cls, times=3))):
        return {"type": type(value).__name__, "times": value.times}

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/annotated", "/default"):
            assert version_client.get(path).json() == {"type": resource_cls.__name__, "times": 3}


@pytest.mark.parametrize("is_async", [False, True])
def test__router_generation__partial_dependency__should_keep_annotations_of_wrapped_function(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    is_async: bool,
):
    def get_user() -> str:
        return "alice"

    def require_role(role: str, user: Annotated[str, Depends(get_user)], body: SchemaWithOneIntField):
        return {"role": role, "user": user, "body": body.model_dump()}

    async def async_require_role(role: str, user: Annotated[str, Depends(get_user)], body: SchemaWithOneIntField):
        return require_role(role, user, body)

    role_dependency = async_require_role if is_async else require_role

    @router.post("/annotated")
    async def annotated_route(value: Annotated[Any, Depends(functools.partial(role_dependency, "admin"))]):
        return value

    @router.post("/default")
    async def default_route(value: Any = Depends(functools.partial(role_dependency, "admin"))):
        return value

    clients = create_versioned_clients(
        version_change(schema(SchemaWithOneIntField).field("bar").existed_as(type=str)),
    )
    for path in ("/default", "/annotated"):
        assert clients["2000-01-01"].post(path, json={"foo": 1}).json() == {
            "detail": [{"type": "missing", "loc": ["body", "bar"], "msg": "Field required", "input": {"foo": 1}}]
        }
        assert clients["2000-01-01"].post(path, json={"foo": 1, "bar": "baz"}).json() == {
            "role": "admin",
            "user": "alice",
            "body": {"foo": 1},
        }
        assert clients["2001-01-01"].post(path, json={"foo": 1}).json() == {
            "role": "admin",
            "user": "alice",
            "body": {"foo": 1},
        }

        clients["2001-01-01"].app.dependency_overrides[get_user] = lambda: "bob"
        assert clients["2000-01-01"].post(path, json={"foo": 1, "bar": "baz"}).json()["user"] == "bob"
        assert clients["2001-01-01"].post(path, json={"foo": 1}).json()["user"] == "bob"
        clients["2001-01-01"].app.dependency_overrides.clear()

        for version, version_client in clients.items():
            operation = version_client.get(f"/openapi.json?version={version}").json()["paths"][path]["post"]
            assert [param["name"] for param in operation["parameters"]] == ["x-api-version"]
            assert operation["requestBody"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/SchemaWithOneIntField"
            }


class _CachedFactory:
    @functools.lru_cache  # noqa: B019
    def __call__(self) -> int:
        return 42


def test__router_generation__cached_callable_default_factory__should_work(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    class Payload(BaseModel):
        value: int = Field(default_factory=_CachedFactory())

    @router.post("/test")
    async def route(body: Payload):
        return body.value

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.post("/test", json={}).json() == 42


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test__router_generation__annotated_metadata_whose_signature_parameters_raise__should_be_kept(
    error_type: type[Exception],
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    class OpaqueSignature(inspect.Signature):
        @property
        @override
        def parameters(self) -> Any:
            raise error_type("opaque signature")

    class Metadata:
        __signature__ = OpaqueSignature()

        def __call__(self) -> None: ...

    class Payload(BaseModel):
        value: Annotated[int, Metadata()]

    @router.post("/test")
    async def route(body: Payload):
        return body.value

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.post("/test", json={"value": 5}).json() == 5


class _ModelWithUnsignaturedDefaultFactory(BaseModel):
    # inspect.signature() fails for builtins such as next() (and time.time() before Python 3.13)
    value: Any = Field(default_factory=functools.partial(next, itertools.count(1)))


def _get_model_with_unsignatured_default_factory(multiplier: int = 1) -> _ModelWithUnsignaturedDefaultFactory:
    return _ModelWithUnsignaturedDefaultFactory(value=multiplier)


def test__router_generation__dependency_return_annotations__should_not_be_versioned(
    create_versioned_clients: CreateVersionedClients,
):
    # FastAPI never looks at the return annotations of dependencies so Cadwyn must not generate their models either
    cached_dependency = functools.lru_cache(_get_model_with_unsignatured_default_factory)
    partial_dependency = functools.partial(_get_model_with_unsignatured_default_factory, 2)
    router = VersionedAPIRouter(dependencies=[Depends(cached_dependency), Depends(partial_dependency)])

    @router.get("/annotated")
    async def annotated_route(
        cached: Annotated[Any, Depends(cached_dependency)], partial: Annotated[Any, Depends(partial_dependency)]
    ):
        return [cached.value, partial.value]

    @router.get("/default")
    async def default_route(cached: Any = Depends(cached_dependency), partial: Any = Depends(partial_dependency)):
        return [cached.value, partial.value]

    for version_client in create_versioned_clients(version_change(), router=router).values():
        assert version_client.get("/annotated").json() == [1, 2]
        assert version_client.get("/default").json() == [1, 2]


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> int:
        self.count += 1
        return self.count


def test__schema_generation__deep_copies_of_versioned_default_factories__should_copy_the_factories_too():
    class ModelWithStatefulDefaultFactory(BaseModel):
        count: int = Field(default_factory=_Counter())

    generators = generate_versioned_models(VersionBundle(Version("2001-01-01"), Version("2000-01-01")))
    for generator in generators.values():
        field = generator[ModelWithStatefulDefaultFactory].model_fields["count"]
        count_before_copying = field.get_default(call_default_factory=True)
        copy.deepcopy(field).get_default(call_default_factory=True)
        assert field.get_default(call_default_factory=True) == count_before_copying + 1


class _PartialWithOverriddenCall(functools.partial):
    @override
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return super().__call__(*args, **kwargs) + 100


class _PartialWithOverriddenCallAndForwardedAttributes(_PartialWithOverriddenCall):
    # Forwards __globals__ of .func too
    def __getattr__(self, name: str) -> Any:
        return getattr(self.func, name)


class _PartialWithOverriddenCallAndHiddenDict(_PartialWithOverriddenCall):
    @override
    def __getattribute__(self, name: str) -> Any:
        if name == "__dict__":
            raise AssertionError("Cadwyn must not depend on the __dict__ attribute of the dependency")
        return super().__getattribute__(name)


def _get_seven() -> int:
    return 7


@pytest.mark.parametrize(
    "partial_subclass",
    [
        _PartialWithOverriddenCall,
        _PartialWithOverriddenCallAndForwardedAttributes,
        _PartialWithOverriddenCallAndHiddenDict,
    ],
)
def test__router_generation__partial_subclass_dependency_with_overridden_call__should_call_the_override(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    partial_subclass: type[functools.partial[int]],
):
    # The instance must not have any attributes. Otherwise, Python would not flatten it into the partials around it
    dependency = partial_subclass(_get_seven)

    @router.get("/default")
    def default_route(value: int = Depends(dependency)):
        return value

    @router.get("/annotated")
    def annotated_route(value: Annotated[int, Depends(dependency)]):
        return value

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.get("/default").json() == 107
        assert version_client.get("/annotated").json() == 107


class _ProxyPretendingToBeItsTarget:
    def __init__(self, target: Callable[..., Any]) -> None:
        self.target = target

    @property
    @override
    def __class__(self) -> type:
        return self.target.__class__

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.target(*args, **kwargs)


def test__router_generation__proxy_of_partial_dependency__should_be_called_like_in_head(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.get("/test")
    def route(value: int = Depends(_ProxyPretendingToBeItsTarget(functools.partial(_get_seven)))):
        return value

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.get("/test").json() == 7


def test__router_generation__dependency_with_lazy_type_checking_only_annotations__should_resolve_the_other_ones(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    paths = []
    # Python 3.10-3.13 evaluate annotations when a function gets defined so such dependencies cannot exist there
    if sys.version_info >= (3, 14):
        lazy_dependencies = importlib.import_module("tests._data.lazy_type_checking_dependencies")

        @router.get("/return")
        async def return_route(limit: Annotated[int, Depends(lazy_dependencies.get_limit)]):
            return limit

        @router.get("/parameter")
        async def parameter_route(limit: Annotated[int, Depends(lazy_dependencies.get_limit_with_price)]):
            return limit

        paths += ["/return", "/parameter"]
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.get(path, params={"limit": "5"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == 5
            assert version_client.get(path, params={"limit": "five"}).status_code == 422


def test__router_generation__dependency_with_parameters_only_in_signature__should_keep_their_types(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    def dynamic_dependency(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    # Some libraries build dependencies dynamically and only describe their parameters through __signature__
    dynamic_dependency.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]  # Set dynamically.
        [
            inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY, default=Query(ge=1), annotation=int),
            inspect.Parameter("body", inspect.Parameter.KEYWORD_ONLY, annotation=SchemaWithOneIntField),
        ]
    )

    @router.post("/test")
    async def route(value: Any = Depends(dynamic_dependency)):
        return value

    clients = create_versioned_clients(
        version_change(schema(SchemaWithOneIntField).field("bar").existed_as(type=str)),
    )
    bodies = {"2000-01-01": {"foo": 1, "bar": "baz"}, "2001-01-01": {"foo": 1}}
    for version, version_client in clients.items():
        response = version_client.post("/test", params={"limit": 5}, json=bodies[version])
        assert response.json() == {"limit": 5, "body": {"foo": 1}}
        response = version_client.post("/test", params={"limit": 0}, json=bodies[version])
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["query", "limit"]

        operation = version_client.get(f"/openapi.json?version={version}").json()["paths"]["/test"]["post"]
        assert operation["parameters"][0] == {
            "name": "limit",
            "in": "query",
            "required": True,
            "schema": {"type": "integer", "minimum": 1, "title": "Limit"},
        }
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/SchemaWithOneIntField"
        }

    assert clients["2000-01-01"].post("/test", params={"limit": 5}, json={"foo": 1}).status_code == 422


def test__router_generation__annotated_dependency_with_unversioned_type__should_use_dependency_of_its_version(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    class Color(str, Enum):
        red = "red"
        blue = "blue"

    class Item(BaseModel):
        name: str
        color: Color

    def get_name_lengths(body: Item) -> list[int]:
        return [len(body.name)]

    @router.post("/dependency")
    async def dependency_route(lengths: Annotated[list[int], Depends(get_name_lengths)]):
        return lengths

    @router.post("/body")
    async def body_route(body: Item):
        return [len(body.name)]

    clients = create_versioned_clients(
        version_change(schema(Item).field("name").had(min_length=10)),
        version_change(),
        head_version_changes=[
            version_change(schema(Item).field("name").had(min_length=5), enum(Color).didnt_have("blue")),
        ],
    )
    min_lengths = {"2002-01-01": 5, "2001-01-01": 5, "2000-01-01": 10}
    for version, version_client in clients.items():
        min_length = min_lengths[version]
        for path in ("/dependency", "/body"):
            response = version_client.post(path, json={"name": "a" * min_length, "color": "red"})
            assert response.json() == [min_length]
            response = version_client.post(path, json={"name": "a" * (min_length - 1), "color": "red"})
            assert response.status_code == 422
            response = version_client.post(path, json={"name": "a" * min_length, "color": "blue"})
            assert response.status_code == 422

        openapi = version_client.get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["Item"]["properties"]["name"] == {
            "type": "string",
            "minLength": min_length,
            "title": "Name",
        }
        assert openapi["components"]["schemas"]["Color"]["enum"] == ["red"]
        for path in ("/dependency", "/body"):
            assert openapi["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/Item"
            }


class RoleOfVersionedDependency(str, Enum):
    reader = "reader"
    editor = "editor"


def get_allowed_roles(
    role: RoleOfVersionedDependency = Query(RoleOfVersionedDependency.reader),
) -> list[str]:
    return [role.value]


def get_allowed_roles_with_sub_dependency(roles: Annotated[list[str], Depends(get_allowed_roles)]) -> list[str]:
    return roles


def get_allowed_roles_with_scopes(
    security_scopes: SecurityScopes,
    role: RoleOfVersionedDependency = Query(RoleOfVersionedDependency.reader),
) -> list[str]:
    return [role.value, *security_scopes.scopes]


AllowedRoles: TypeAlias = Annotated[list[str], Depends(get_allowed_roles)]


def _make_permissions_route(style: str) -> Callable[..., Any]:
    if style == "default":

        async def default_route(roles: list[str] = Depends(get_allowed_roles)):
            return roles

        return default_route
    if style == "annotated":

        async def annotated_route(roles: Annotated[list[str], Depends(get_allowed_roles)]):
            return roles

        return annotated_route
    if style == "annotated_with_other_metadata":

        async def annotated_with_other_metadata_route(
            roles: Annotated[list[str], "roles", Depends(get_allowed_roles)],
        ):
            return roles

        return annotated_with_other_metadata_route
    if style == "annotated_alias":

        async def annotated_alias_route(roles: AllowedRoles):
            return roles

        return annotated_alias_route
    if style == "nested_annotated_dependency":

        async def nested_annotated_dependency_route(
            roles: Annotated[list[str], Depends(get_allowed_roles_with_sub_dependency)],
        ):
            return roles

        return nested_annotated_dependency_route
    if style == "annotated_security":

        async def annotated_security_route(roles: Annotated[list[str], Security(get_allowed_roles_with_scopes)]):
            return roles

        return annotated_security_route
    assert style == "annotated_security_with_scopes"

    async def annotated_security_with_scopes_route(
        roles: Annotated[list[str], Security(get_allowed_roles_with_scopes, scopes=["roles:read"])],
    ):
        return roles

    return annotated_security_with_scopes_route


@pytest.mark.parametrize(
    "style",
    [
        "default",
        "annotated",
        "annotated_with_other_metadata",
        "annotated_alias",
        "nested_annotated_dependency",
        "annotated_security",
        "annotated_security_with_scopes",
    ],
)
def test__router_generation__dependency_with_versioned_enum_in_its_params__should_use_dependency_of_its_version(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    style: str,
):
    router.get("/permissions")(_make_permissions_route(style))
    clients = create_versioned_clients(version_change(enum(RoleOfVersionedDependency).didnt_have("editor")))
    scopes = ["roles:read"] if style == "annotated_security_with_scopes" else []

    assert clients["2001-01-01"].get("/permissions", params={"role": "editor"}).json() == ["editor", *scopes]
    assert clients["2000-01-01"].get("/permissions", params={"role": "reader"}).json() == ["reader", *scopes]
    assert clients["2000-01-01"].get("/permissions", params={"role": "editor"}).status_code == 422
    for version, enum_values in {"2001-01-01": ["reader", "editor"], "2000-01-01": ["reader"]}.items():
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["RoleOfVersionedDependency"]["enum"] == enum_values


class PlanOfDependency(Enum):
    free = "free"
    pro = "pro"
    team = "team"


PRICES_OF_PLANS = {PlanOfDependency.free: 0, PlanOfDependency.pro: 10, PlanOfDependency.team: 30}


class AddonOfDependency(BaseModel):
    plan: PlanOfDependency


class SubscriptionOfDependency(BaseModel):
    # Has no instructions itself but refers to an enum that has them, directly and through another model
    plan: PlanOfDependency
    addons: list[AddonOfDependency] = []


class UnrelatedModelOfDependency(BaseModel):
    foo: str


def get_price_from_query(plan: PlanOfDependency = Query(PlanOfDependency.free)) -> int:
    return PRICES_OF_PLANS[plan]


def get_price_from_body(subscription: SubscriptionOfDependency) -> int:
    assert isinstance(subscription, SubscriptionOfDependency)
    return PRICES_OF_PLANS[subscription.plan]


def get_price_from_sub_dependency(price: Annotated[int, Depends(get_price_from_query)]) -> int:
    return price


_PRICE_DEPENDENCIES: dict[str, tuple[Callable[..., int], Callable[[TestClient, str], Any]]] = {
    "query": (get_price_from_query, lambda client, plan: client.get("/price", params={"plan": plan})),
    "body": (get_price_from_body, lambda client, plan: client.post("/price", json={"plan": plan})),
    "sub_dependency": (
        get_price_from_sub_dependency,
        lambda client, plan: client.get("/price", params={"plan": plan}),
    ),
}


def _add_price_route(router: VersionedAPIRouter, dependency: Callable[..., int]) -> None:
    async def route(price: Annotated[int, Depends(dependency)]):
        return price

    method = "POST" if dependency is get_price_from_body else "GET"
    router.api_route("/price", methods=[method])(route)


@pytest.mark.parametrize("source", list(_PRICE_DEPENDENCIES))
@pytest.mark.parametrize(
    "make_version_change",
    [
        pytest.param(version_change, id="no_instructions"),
        pytest.param(
            lambda: version_change(schema(UnrelatedModelOfDependency).field("foo").had(name="bar")),
            id="unrelated_instructions",
        ),
    ],
)
def test__router_generation__annotated_dependency_without_changed_models__should_get_original_classes(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    source: str,
    make_version_change: Callable[[], type[VersionChange]],
):
    dependency, send = _PRICE_DEPENDENCIES[source]
    _add_price_route(router, dependency)

    clients = create_versioned_clients(make_version_change())
    for version_client in clients.values():
        # The dependency gets the original classes, so lookups by enum members and isinstance work
        assert send(version_client, "pro").json() == 10
        assert send(version_client, "team").json() == 30


@pytest.mark.parametrize("source", list(_PRICE_DEPENDENCIES))
def test__router_generation__annotated_dependency_with_changed_enum__should_be_versioned_only_where_it_changes(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    source: str,
):
    dependency, send = _PRICE_DEPENDENCIES[source]
    _add_price_route(router, dependency)

    clients = create_versioned_clients(version_change(enum(PlanOfDependency).didnt_have("team")))
    # The latest version validates the same as head so the dependency gets the original classes there
    assert send(clients["2001-01-01"], "pro").json() == 10
    assert send(clients["2001-01-01"], "team").json() == 30
    # The older version uses its own copy of the dependency, which rejects the plan that it did not have
    assert send(clients["2000-01-01"], "team").status_code == 422
    for version, enum_values in {"2001-01-01": ["free", "pro", "team"], "2000-01-01": ["free", "pro"]}.items():
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["PlanOfDependency"]["enum"] == enum_values


def get_default_role_name() -> RoleOfVersionedDependency:
    return RoleOfVersionedDependency.reader


class UserWithDefaultFactoryOfChangedEnum(BaseModel):
    # Only the default factory refers to the changed enum so it does not change what the model validates
    role_name: str = Field(default_factory=get_default_role_name)


class ParentWithChangedEnum(BaseModel):
    role_name: RoleOfVersionedDependency


class ChildThatOverridesChangedEnum(ParentWithChangedEnum):
    role_name: str  # ty: ignore[invalid-attribute-override]


class ModelWithChangedEnumInClassVar(BaseModel):
    default_role: ClassVar[RoleOfVersionedDependency] = RoleOfVersionedDependency.reader
    role_name: str


_GenericArgT = TypeVar("_GenericArgT")


class GenericThatDoesNotUseItsArg(BaseModel, Generic[_GenericArgT]):
    role_name: str


class GenericThatUsesItsArg(BaseModel, Generic[_GenericArgT]):
    role_name: _GenericArgT


AliasThatDoesNotUseItsArg = TypeAliasType("AliasThatDoesNotUseItsArg", str, type_params=(_GenericArgT,))
AliasThatUsesItsArg = TypeAliasType("AliasThatUsesItsArg", Union[_GenericArgT, None], type_params=(_GenericArgT,))


class ModelWithUnusedAliasArg(BaseModel):
    role_name: AliasThatDoesNotUseItsArg[RoleOfVersionedDependency]


class ModelWithUsedAliasArg(BaseModel):
    role_name: AliasThatUsesItsArg[RoleOfVersionedDependency]


AliasThatIsItsArg = TypeAliasType("AliasThatIsItsArg", _GenericArgT, type_params=(_GenericArgT,))


class ModelWithAliasThatIsItsArg(BaseModel):
    role_name: AliasThatIsItsArg[RoleOfVersionedDependency]


class ModelWithNestedUnusedAliasArg(BaseModel):
    role_name: str
    other_role_names: list[AliasThatDoesNotUseItsArg[RoleOfVersionedDependency]] | None = None


class ModelWithUntypedExtras(BaseModel):
    model_config = ConfigDict(extra="allow")
    role_name: str


AliasOfAliasThatDoesNotUseItsArg = TypeAliasType(
    "AliasOfAliasThatDoesNotUseItsArg",
    AliasThatDoesNotUseItsArg[RoleOfVersionedDependency],
)


class ModelWithUnusedArgOfAliasBehindAlias(BaseModel):
    role_name: AliasOfAliasThatDoesNotUseItsArg


if sys.version_info >= (3, 12):
    # Type aliases that refer to themselves can only be defined with the type statement
    exec(
        "type RecursiveAlias[T] = T | list[RecursiveAlias[T]]\n"
        "type RecursiveAliasThatDoesNotUseItsArg[T] = str | list[RecursiveAliasThatDoesNotUseItsArg[T]]\n"
        "class ModelWithRecursiveAliasThatDoesNotUseItsArg(BaseModel):\n"
        "    role_name: RecursiveAliasThatDoesNotUseItsArg[RoleOfVersionedDependency]\n"
        "class ModelWithRecursiveAliasOfUnchangedArg(BaseModel):\n"
        "    role_name: RecursiveAlias[str]\n"
        "class ModelWithRecursiveAliasOfChangedArg(BaseModel):\n"
        "    role_name: RecursiveAlias[RoleOfVersionedDependency]\n",
        globals(),
    )
_requires_type_statement = pytest.mark.skipif(sys.version_info < (3, 12), reason="Needs the type statement")


class ModelWithTypedExtras(BaseModel):
    model_config = ConfigDict(extra="allow")
    # Pydantic documents typing extra fields this way, but type checkers see it as narrowing a writable attribute
    if not TYPE_CHECKING:
        __pydantic_extra__: dict[str, RoleOfVersionedDependency] = Field(init=False)

    @property
    def role_name(self) -> RoleOfVersionedDependency:
        return self.model_extra["role_name"]  # ty: ignore[not-subscriptable]


def _make_body_dependency(model: Any) -> Callable[..., str]:
    def dependency(body: Any) -> str:
        assert isinstance(body, model)
        return body.role_name

    dependency.__annotations__["body"] = model
    return dependency


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(ChildThatOverridesChangedEnum, id="overridden_field"),
        pytest.param(ModelWithChangedEnumInClassVar, id="class_var"),
        pytest.param(GenericThatDoesNotUseItsArg[RoleOfVersionedDependency], id="unused_generic_arg"),
        pytest.param(UserWithDefaultFactoryOfChangedEnum, id="default_factory"),
        pytest.param(ModelWithUnusedAliasArg, id="unused_generic_alias_arg"),
        pytest.param(ModelWithNestedUnusedAliasArg, id="nested_unused_generic_alias_arg"),
        pytest.param(ModelWithUntypedExtras, id="untyped_extras"),
        pytest.param(ModelWithUnusedArgOfAliasBehindAlias, id="unused_generic_alias_arg_behind_alias"),
        pytest.param(
            "ModelWithRecursiveAliasOfUnchangedArg", id="recursive_generic_alias", marks=_requires_type_statement
        ),
        pytest.param(
            "ModelWithRecursiveAliasThatDoesNotUseItsArg",
            id="recursive_generic_alias_that_does_not_use_its_arg",
            marks=_requires_type_statement,
        ),
    ],
)
def test__router_generation__annotated_dependency_whose_models_do_not_validate_changed_enum__should_not_be_versioned(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    model: Any,
):
    model = globals()[model] if isinstance(model, str) else model

    @router.post("/role")
    async def route(role_name: Annotated[str, Depends(_make_body_dependency(model))]):
        return role_name

    # HeadVersion changes the enum in every version, including the latest one
    clients = create_versioned_clients(
        version_change(),
        head_version_changes=[version_change(enum(RoleOfVersionedDependency).didnt_have("editor"))],
    )
    # The models refer to the changed enum without validating it so the dependency gets the original classes
    for version_client in clients.values():
        assert version_client.post("/role", json={"role_name": "anything"}).json() == "anything"
        if model is UserWithDefaultFactoryOfChangedEnum:
            assert version_client.post("/role", json={}).json() == "reader"


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(ParentWithChangedEnum, id="field"),
        pytest.param(GenericThatUsesItsArg[RoleOfVersionedDependency], id="used_generic_arg"),
        pytest.param(ModelWithUsedAliasArg, id="used_generic_alias_arg"),
        pytest.param(ModelWithAliasThatIsItsArg, id="generic_alias_that_is_its_arg"),
        pytest.param(ModelWithTypedExtras, id="typed_extras"),
        pytest.param(
            "ModelWithRecursiveAliasOfChangedArg", id="recursive_generic_alias", marks=_requires_type_statement
        ),
    ],
)
def test__router_generation__annotated_dependency_whose_models_validate_changed_enum__should_be_versioned(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    model: Any,
):
    model = globals()[model] if isinstance(model, str) else model

    async def dependency(body: Any) -> str:
        return body.role_name.value

    dependency.__annotations__["body"] = model

    @router.post("/role")
    async def route(role_name: Annotated[str, Depends(dependency)]):
        return role_name

    clients = create_versioned_clients(version_change(enum(RoleOfVersionedDependency).didnt_have("editor")))
    assert clients["2001-01-01"].post("/role", json={"role_name": "editor"}).json() == "editor"
    assert clients["2000-01-01"].post("/role", json={"role_name": "editor"}).status_code == 422
    assert clients["2000-01-01"].post("/role", json={"role_name": "reader"}).json() == "reader"


def test__router_generation__annotated_dependency_whose_base_has_instructions__should_be_versioned(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    async def dependency(body: ChildThatOverridesChangedEnum) -> str:
        return body.role_name

    @router.post("/role")
    async def route(role_name: Annotated[str, Depends(dependency)]):
        return role_name

    clients = create_versioned_clients(
        version_change(schema(ParentWithChangedEnum).field("legacy").existed_as(type=str)),
    )
    # The copy of the child inherits the field from the copy of its parent
    assert clients["2001-01-01"].post("/role", json={"role_name": "a"}).json() == "a"
    assert clients["2000-01-01"].post("/role", json={"role_name": "a"}).status_code == 422
    assert clients["2000-01-01"].post("/role", json={"role_name": "a", "legacy": "b"}).json() == "a"


def test__router_generation__annotated_dependency_with_unused_alias_arg_in_its_params__should_not_be_versioned(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    async def dependency(
        body: UnrelatedModelOfDependency,
        unused: AliasThatDoesNotUseItsArg[RoleOfVersionedDependency] = "x",
    ) -> str:
        assert isinstance(body, UnrelatedModelOfDependency)
        return body.foo

    @router.post("/foo")
    async def route(foo: Annotated[str, Depends(dependency)]):
        return foo

    clients = create_versioned_clients(
        version_change(),
        head_version_changes=[version_change(enum(RoleOfVersionedDependency).didnt_have("editor"))],
    )
    for version_client in clients.values():
        assert version_client.post("/foo", json={"foo": "bar"}).json() == "bar"


def test__router_generation__annotated_dependency_whose_base_changes_only_overridden_fields__should_not_be_versioned(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.post("/role")
    async def route(role_name: Annotated[str, Depends(_make_body_dependency(ChildThatOverridesChangedEnum))]):
        return role_name

    clients = create_versioned_clients(
        version_change(),
        head_version_changes=[version_change(schema(ParentWithChangedEnum).field("role_name").had(type=int))],
    )
    # The child overrides the only field that the instructions of its parent change
    for version_client in clients.values():
        assert version_client.post("/role", json={"role_name": "anything"}).json() == "anything"


def test__get_value_of_type_alias_with_args__alias_that_cannot_be_substituted__should_be_unresolved():
    variadic_args = TypeVarTuple("variadic_args")
    variadic_alias = TypeAliasType("variadic_alias", tuple[Unpack[variadic_args]], type_params=(variadic_args,))
    assert _get_value_of_type_alias_with_args(variadic_alias[int, str]) is _UNRESOLVED


def get_role_name_from_query(role: RoleOfVersionedDependency = Query(RoleOfVersionedDependency.reader)) -> str:
    return role.value


def test__router_generation__inferred_annotated_dependency__should_use_dependency_of_its_version(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.get("/role")
    async def route(role_name: Annotated[get_role_name_from_query, Depends()]):  # ty: ignore[invalid-type-form]
        return role_name

    clients = create_versioned_clients(version_change(enum(RoleOfVersionedDependency).didnt_have("editor")))
    assert clients["2001-01-01"].get("/role", params={"role": "editor"}).json() == "editor"
    assert clients["2000-01-01"].get("/role", params={"role": "reader"}).json() == "reader"
    assert clients["2000-01-01"].get("/role", params={"role": "editor"}).status_code == 422
    for version, enum_values in {"2001-01-01": ["reader", "editor"], "2000-01-01": ["reader"]}.items():
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["RoleOfVersionedDependency"]["enum"] == enum_values


def test__router_generation__annotated_dependency_with_enum_changed_by_head_version__should_be_versioned_in_latest(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
):
    @router.get("/permissions")
    async def route(roles: Annotated[list[str], Depends(get_allowed_roles)]):
        return roles

    clients = create_versioned_clients(
        head_version_changes=[version_change(enum(RoleOfVersionedDependency).didnt_have("editor"))]
    )
    # HeadVersion changes the latest published version too, so it is versioned there
    client = clients["2000-01-01"]
    assert client.get("/permissions", params={"role": "reader"}).json() == ["reader"]
    assert client.get("/permissions", params={"role": "editor"}).status_code == 422
    openapi = client.get("/openapi.json?version=2000-01-01").json()
    assert openapi["components"]["schemas"]["RoleOfVersionedDependency"]["enum"] == ["reader"]


class ItemOfDependencyWithQuotedAnnotations(BaseModel):
    name: str


class RoleOfDependencyWithQuotedAnnotations(str, Enum):
    reader = "reader"
    editor = "editor"


def get_tags(items: list["ItemOfDependencyWithQuotedAnnotations"] = Body([])) -> list[str]:
    return [item.name for item in items]


def get_roles_with_quoted_query(
    role: RoleOfDependencyWithQuotedAnnotations = Query(RoleOfDependencyWithQuotedAnnotations.reader),
    tags: list["str"] = Query([]),
) -> list[str]:
    return [role.value]


def get_roles_with_quoted_body(
    role: RoleOfDependencyWithQuotedAnnotations = Query(RoleOfDependencyWithQuotedAnnotations.reader),
    item: Optional["ItemOfDependencyWithQuotedAnnotations"] = Body(None),
) -> list[str]:
    return [role.value]


def get_roles_with_quoted_sub_dependency(
    tags: Annotated[list[str], Depends(get_tags)],
    role: RoleOfDependencyWithQuotedAnnotations = Query(RoleOfDependencyWithQuotedAnnotations.reader),
) -> list[str]:
    return [role.value]


@pytest.mark.parametrize(
    "dependency",
    [get_roles_with_quoted_query, get_roles_with_quoted_body, get_roles_with_quoted_sub_dependency],
)
def test__router_generation__annotated_dependency_with_quoted_args_in_annotations__should_use_dependency_of_its_version(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    dependency: Callable[..., list[str]],
):
    @router.get("/roles")
    async def route(roles: Annotated[list[str], Depends(dependency)]):
        return roles

    clients = create_versioned_clients(
        version_change(enum(RoleOfDependencyWithQuotedAnnotations).didnt_have("editor")),
    )
    assert clients["2001-01-01"].get("/roles", params={"role": "editor"}).json() == ["editor"]
    assert clients["2000-01-01"].get("/roles", params={"role": "reader"}).json() == ["reader"]
    assert clients["2000-01-01"].get("/roles", params={"role": "editor"}).status_code == 422
    if dependency is not get_roles_with_quoted_query:
        # Pydantic cannot resolve the forward refs to the models in the OpenAPI, just like without Cadwyn
        return
    for version, enum_values in {"2001-01-01": ["reader", "editor"], "2000-01-01": ["reader"]}.items():
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["RoleOfDependencyWithQuotedAnnotations"]["enum"] == enum_values


class _MetadataWhoseEqualityRaises:
    # Callable metadata gets versioned, so typing's Annotated cache would compare it with its versioned copy
    def __call__(self) -> None:
        raise NotImplementedError

    @override
    def __hash__(self) -> int:
        return 123

    @override
    def __eq__(self, other: object) -> bool:
        raise TypeError("metadata cannot be compared")


class _MetadataWithChangingHash:
    counter = 0

    @override
    def __hash__(self) -> int:
        self.counter += 1
        return self.counter


class PayloadOfAnnotatedDependency(BaseModel):
    value: str


def get_payload(payload: PayloadOfAnnotatedDependency) -> dict[str, Any]:
    return payload.model_dump()


def _make_dependency_with_many_annotated_parameters() -> Callable[..., Any]:
    # More distinct Annotated[...] than typing caches
    parameters = ", ".join(f'p{i}: Annotated[int, "parameter {i}"] = 0' for i in range(150))
    namespace: dict[str, Any] = {"Annotated": Annotated, "get_payload": get_payload}
    payload_annotation = "get_payload.__annotations__['payload']"
    exec(f"def dependency(payload: {payload_annotation}, {parameters}):\n    return get_payload(payload)", namespace)
    return namespace["dependency"]


@pytest.mark.parametrize(
    "make_annotation",
    [
        pytest.param(lambda: Annotated[dict, Depends(get_payload)], id="plain"),
        pytest.param(lambda: Annotated[dict, Depends(get_payload), {"description": "Value"}], id="unhashable_metadata"),
        pytest.param(lambda: Annotated[dict, Depends(get_payload), _MetadataWhoseEqualityRaises()], id="eq_raises"),
        pytest.param(lambda: Annotated[dict, Depends(get_payload), _MetadataWithChangingHash()], id="changing_hash"),
        pytest.param(
            lambda: Annotated[dict, Depends(_make_dependency_with_many_annotated_parameters())],
            id="many_annotated_parameters",
        ),
    ],
)
def test__router_generation__annotated_dependency_with_any_metadata__should_use_dependency_of_its_version(
    router: VersionedAPIRouter,
    create_versioned_clients: CreateVersionedClients,
    make_annotation: Callable[[], Any],
):
    annotation = make_annotation()

    async def route(value: Any):
        return value

    route.__annotations__["value"] = annotation
    router.post("/value")(route)

    @convert_request_to_next_version_for("/value", ["POST"])
    def migrate(request: RequestInfo):
        request.body["value"] = request.body.pop("legacy_value")

    clients = create_versioned_clients(
        version_change(schema(PayloadOfAnnotatedDependency).field("value").had(name="legacy_value"), migrate=migrate),
    )
    # Whether typing would cache Annotated[...] with these args or not, every version gets its own dependency
    assert clients["2001-01-01"].post("/value", json={"value": "ok"}).json() == {"value": "ok"}
    assert clients["2000-01-01"].post("/value", json={"legacy_value": "ok"}).json() == {"value": "ok"}
    for version, field_name in [("2001-01-01", "value"), ("2000-01-01", "legacy_value")]:
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert list(openapi["components"]["schemas"]["PayloadOfAnnotatedDependency"]["properties"]) == [field_name]


def test__router_generation__with_pydantic_settings_dependency(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
) -> None:
    class Settings(BaseSettings):
        foo: str = Field(default="bar")

    async def settings() -> Settings:
        return Settings()

    @router.get("/test")
    def read_root(settings: Annotated[Settings, Depends(settings)]):
        assert settings
        return {"foo": settings.foo}

    app = create_versioned_app(version_change())
    with TestClient(app) as client:
        resp = client.get("/test", headers={"x-api-version": "2000-01-01"})
        assert resp.status_code == 200
        assert resp.json() == {"foo": "bar"}

        resp = client.get("/test", headers={"x-api-version": "2001-01-01"})
        assert resp.status_code == 200
        assert resp.json() == {"foo": "bar"}


######################
# External lib testing
######################


def test__router_generation__using_svcs_in_dependencies(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    # This makes sure that svcs is compatible with Cadwyn
    @router.api_route("/test")
    async def test(my_dep: svcs.fastapi.DepContainer):
        raise NotImplementedError

    # We are simply validating that the routes were generated without errors
    routes_2000, routes_2001 = create_versioned_api_routes(version_change())
    assert len(routes_2000) == len(routes_2001) == 2


def test__copy_route__when_route_has_no_flat_dependant__then_copies_route():
    route = APIRoute("/test", lambda: None)
    flat_dependant_attr = "_flat_dependant"
    setattr(route, flat_dependant_attr, route.dependant)
    delattr(route, flat_dependant_attr)

    copied = copy_route(route)

    assert copied.path == route.path
    assert not hasattr(copied, flat_dependant_attr)


def test__copy_route__when_route_has_flat_dependant__then_copies_flat_dependant():
    route = APIRoute("/test", lambda: None)
    flat_dependant = route.dependant
    flat_dependant_attr = "_flat_dependant"
    setattr(route, flat_dependant_attr, flat_dependant)

    copied = copy_route(route)

    copied_flat_dependant = getattr(copied, flat_dependant_attr)
    assert copied_flat_dependant is not flat_dependant
    assert copied_flat_dependant.call is flat_dependant.call


def test__router_generation__sqlalchemy_models__should_preserve_classes_and_serve_all_versions(
    router: VersionedAPIRouter, create_versioned_clients: CreateVersionedClients
):
    user = MappedModel(name="alice")

    def get_user() -> MappedModel:
        return user

    @router.get("/user", response_model=MappedModel)
    def get_user_route(value: MappedModel = Depends(get_user)):
        assert value is user
        return value

    @router.get("/users", response_model=list[MappedModel])
    def get_users_route(value: Annotated[MappedModel, Depends(get_user)]):
        assert value is user
        return [value]

    child = MappedChild(name="alice")
    orm_user = ImperativelyMappedModel()
    orm_user.id = 1
    orm_user.name = "alice"

    def get_child() -> MappedChild:
        return child

    def get_orm_user() -> ImperativelyMappedModel:
        return orm_user

    @router.get("/child", response_model=MappedChild)
    def get_child_route(value: MappedChild = Depends(get_child)):
        assert value is child
        return value

    @router.get("/orm-user")
    def get_orm_user_route(value: ImperativelyMappedModel = Depends(get_orm_user)):
        assert value is orm_user
        return {"id": value.id, "name": value.name}

    @router.post("/unmapped", response_model=ModelFields)
    def unmapped_route(value: ModelFields):
        return value

    clients = create_versioned_clients(version_change())
    assert len(clients) == 2
    for version_client in clients.values():
        response = version_client.get("/user")
        assert response.status_code == 200
        assert response.json() == {"id": 1, "name": "alice"}
        response = version_client.get("/users")
        assert response.status_code == 200
        assert response.json() == [{"id": 1, "name": "alice"}]
        assert version_client.get("/child").json() == {"id": 1, "name": "alice"}
        assert version_client.get("/orm-user").json() == {"id": 1, "name": "alice"}
        response = version_client.post("/unmapped", json={"name": "alice"})
        assert response.status_code == 200
        assert response.json() == {"name": "alice"}

    generators = generate_versioned_models(VersionBundle(Version("2001-01-01"), Version("2000-01-01")))
    for generator in generators.values():
        assert generator[MappedModel] is MappedModel
        assert generator[MappedChild] is MappedChild
        assert generator[ModelFields] is ModelFields


def get_user_from_header(x_user: Annotated[str, Header()]) -> str:
    return x_user


def require_role(role: str, user: Annotated[str, Depends(get_user_from_header)]) -> str:
    if user != role:
        raise HTTPException(status_code=403)
    return user


def get_kwargs(**kwargs: Any) -> dict[str, Any]:
    return kwargs


get_kwargs.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
    [inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY, default=Query(10, ge=1), annotation=int)]
)


def test__router_generation__annotated_dependencies_of_various_kinds__should_work_in_all_versions(
    create_versioned_app: CreateVersionedApp,
):
    events: list[str] = []

    async def get_transaction():
        events.append("start")
        try:
            yield "transaction"
        except HTTPException:
            events.append("rollback")
            raise

    router = VersionedAPIRouter()

    @router.get("/admin")
    def get_admin(user: Annotated[str, Depends(functools.partial(require_role, "admin"))]) -> str:
        return user

    @router.get("/kwargs")
    def get_query_kwargs(kwargs: Annotated[dict, Depends(get_kwargs)]) -> dict:
        return kwargs

    @router.post("/conflict")
    async def create_conflict(transaction: Annotated[str, Depends(get_transaction)]) -> str:
        raise HTTPException(status_code=409)

    app = create_versioned_app(version_change(schema(EmptySchema).had(name="OldEmptySchema")), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/admin", headers={"x-user": "admin"}).json() == "admin"
        assert client.get("/admin", headers={"x-user": "bob"}).status_code == 403
        assert client.get("/kwargs", params={"limit": 3}).json() == {"limit": 3}
        assert client.get("/kwargs", params={"limit": 0}).status_code == 422
        assert client.post("/conflict").status_code == 409
        # Older versions also solve the dependencies of the latest version so they can run twice
        assert events.count("start") == events.count("rollback") > 0
        events.clear()
        parameters = client.get(f"/openapi.json?version={version}").json()["paths"]["/kwargs"]["get"]["parameters"]
        parameter_types = {parameter["name"]: parameter["schema"]["type"] for parameter in parameters}
        assert parameter_types == {"limit": "integer", app.router.api_version_parameter_name: "string"}


class ConstrainedItemState(str, Enum):
    old = "old"
    new = "new"


class ConstrainedItem(BaseModel):
    name: str
    state: ConstrainedItemState = ConstrainedItemState.old


def get_name_lengths(body: ConstrainedItem) -> list[int]:
    return [len(body.name)]


def test__router_generation__annotated_bare_alias_with_dependency_on_versioned_body__should_use_body_from_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/items")
    def create_items(lengths: Annotated[typing.List, Depends(get_name_lengths)]) -> list[int]:  # noqa: UP006
        return lengths

    app = create_versioned_app(
        head_version_changes=[
            version_change(
                schema(ConstrainedItem).field("name").had(min_length=5),
                enum(ConstrainedItemState).didnt_have("new"),
            )
        ],
        router=router,
    )
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert client_2000.post("/items", json={"name": "abcde"}).json() == [5]
    assert client_2000.post("/items", json={"name": "x"}).status_code == 422
    assert client_2000.post("/items", json={"name": "abcde", "state": "new"}).status_code == 422
    old_schemas = TestClient(app).get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert old_schemas["ConstrainedItem"]["properties"]["name"]["minLength"] == 5
    assert old_schemas["ConstrainedItemState"]["enum"] == ["old"]


def test__router_generation__bare_tuple_body__should_keep_its_current_behavior(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/tuples", response_model=typing.Tuple)  # noqa: UP006
    def create_tuple(data: Annotated[typing.Tuple, Body()]):  # noqa: UP006
        return data

    app = create_versioned_app(version_change(schema(EmptySchema).had(name="OldEmptySchema")), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        # Bare aliases are rebuilt as `tuple[()]` in every version
        assert client.post("/tuples", json=[]).json() == []
        assert client.post("/tuples", json=[1]).status_code == 422
        request_body = client.get(f"/openapi.json?version={version}").json()["paths"]["/tuples"]["post"]["requestBody"]
        assert request_body["content"]["application/json"]["schema"]["maxItems"] == 0


def test__router_generation__endpoint_with_annotations_assigned_by_hand__should_get_body_in_all_versions(
    create_versioned_app: CreateVersionedApp,
):
    def create_item(item):  # The annotations are assigned below like in route factories
        return item

    create_item.__annotations__ = {"item": SchemaWithOneIntField, "return": SchemaWithOneIntField}
    router = VersionedAPIRouter()
    router.add_api_route("/items", create_item, methods=["POST"])

    app = create_versioned_app(version_change(schema(EmptySchema).had(name="OldEmptySchema")), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/items", json={"foo": 1}).json() == {"foo": 1}
