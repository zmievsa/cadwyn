import functools
import inspect
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import pytest
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse
from typing_extensions import override

from cadwyn import Cadwyn
from cadwyn.exceptions import CadwynStructureError
from cadwyn.middleware import VersionPickingMiddleware
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.routing import _RootCadwynAPIRouter
from cadwyn.structure.endpoints import endpoint
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import HeadVersion, Version, VersionBundle, VersionChange
from tests._resources.utils import BASIC_HEADERS, DEFAULT_API_VERSION, FASTAPI_ACCEPTS_OPENAPI_PREFIX
from tests._resources.versioned_app.app import (
    client_without_headers,
    client_without_headers_and_with_custom_api_version_var,
    v2021_01_01_router,
    v2022_01_02_router,
)
from tests.conftest import version_change, versions

if TYPE_CHECKING:
    from fastapi.routing import APIRoute

_ADD_HEADER_WARNING = "Use generate_and_include_versioned_routers"


def test__header_routing__invalid_version_format__should_raise_value_error():
    main_app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        main_app.add_header_versioned_routers(  # ty: ignore[deprecated]  # This test verifies the legacy API.
            APIRouter(),
            header_value=DEFAULT_API_VERSION,
        )
    with (
        pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING),
        pytest.raises(ValueError, match=re.escape("header_value should be in ISO 8601 format")),
    ):
        main_app.add_header_versioned_routers(  # ty: ignore[deprecated]  # This test verifies the legacy API.
            APIRouter(),
            header_value="2022-01_01",
        )


def test__header_routing_fastapi_init__openapi_passing_nulls__should_not_add_openapi_routes():
    assert [cast("APIRoute", r).path for r in Cadwyn(versions=VersionBundle(Version("2022-11-16"))).routes] == [
        "/docs/oauth2-redirect",
        "/changelog",
        "/openapi.json",
        "/docs",
        "/redoc",
    ]
    assert [
        cast("APIRoute", r).path
        for r in Cadwyn(versions=VersionBundle(Version("2022-11-16")), docs_url=None, redoc_url=None).routes
    ] == [
        "/changelog",
        "/openapi.json",
    ]
    assert Cadwyn(versions=VersionBundle(Version("2022-11-16")), openapi_url=None, changelog_url=None).routes == []


def test__header_routing_fastapi_init__passing_null_to_oauth2__should_not_add_oauth2_redirect_route():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")), swagger_ui_oauth2_redirect_url=None)
    assert [cast("APIRoute", r).path for r in app.routes] == [
        "/changelog",
        "/openapi.json",
        "/docs",
        "/redoc",
    ]
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2021_01_01_router,
            header_value="2021-01-01",
        )

    with TestClient(app) as client:
        assert client.get("/docs?version=2021-01-01").status_code == 200


def test__header_routing_fastapi_init__changing_openapi_url__docs_still_return_200():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")), openapi_url="/openpapi")
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2021_01_01_router,
            header_value="2021-01-01",
        )
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2022_01_02_router,
            header_value="2022-02-02",
        )
    with TestClient(app) as client:
        assert client.get("/openpapi?version=2021-01-01").status_code == 200
        assert client.get("/openapi.json?version=2021-01-01").status_code == 404


def test__header_routing_fastapi__calling_openapi_incorrectly__docs_should_return_404():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")))
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2021_01_01_router,
            header_value="2021-01-01",
        )
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2022_01_02_router,
            header_value="2022-02-02",
        )
    with TestClient(app) as client:
        assert client.get("/openapi.json?version=2021-01-01").status_code == 200
        # - Nonexisting version
        assert client.get("/openapi.json?version=2019-01-01").status_code == 404
        # - Nonexisting but compatible version
        assert client.get("/openapi.json?version=2024-01-01").status_code == 404
        # - version = null
        assert client.get("/openapi.json?version=").status_code == 404
        # - version not passed at all
        assert client.get("/openapi.json").status_code == 404
        # - Unversioned when we haven't added any
        assert client.get("/openapi.json?version=unversioned").status_code == 404

        @app.post("/my_unversioned_route")
        def my_unversioned_route():
            raise NotImplementedError

        assert client.get("/openapi.json?version=unversioned").status_code == 200


def test__cadwyn__with_dependency_overrides__overrides_should_be_applied():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))

    async def old_dependency():
        raise NotImplementedError

    async def new_dependency():
        return "new"

    @app.post("/hello")
    async def hello(dependency: Annotated[str, Depends(old_dependency)]):
        return dependency

    regular_router = APIRouter()

    @regular_router.post("/darkness")
    async def darkness(dependency: Annotated[str, Depends(old_dependency)]):
        return dependency

    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            regular_router,
            header_value="2022-11-16",
        )

    versioned_router = VersionedAPIRouter()

    @versioned_router.post("/my_old_friend")
    async def my_old_friend(dependency: Annotated[str, Depends(old_dependency)]):
        return dependency

    app.generate_and_include_versioned_routers(versioned_router)

    app.dependency_overrides[old_dependency] = new_dependency
    with TestClient(app) as client:
        assert client.post("/hello").json() == "new"
        assert client.post("/darkness", headers={"x-api-version": "2022-11-16"}).json() == "new"
        assert client.post("/my_old_friend", headers={"x-api-version": "2022-11-16"}).json() == "new"


class _Auth:
    """Every test overrides these dependencies so they must never get called"""

    def __call__(self) -> str:
        raise NotImplementedError

    def method(self) -> str:
        raise NotImplementedError


def _deny(role: str) -> str:
    raise NotImplementedError


_auth = _Auth()


@pytest.mark.parametrize(
    "dependency",
    [_auth, _auth.method, functools.partial(_deny, "admin"), functools.partial(_auth)],
    ids=["instance", "bound_method", "partial", "partial_of_instance"],
)
@pytest.mark.parametrize("override_before_generation", [True, False])
def test__cadwyn__with_dependency_overrides_of_non_function_dependencies__should_be_applied_in_all_versions(
    dependency: Callable[[], str],
    override_before_generation: bool,
):
    router = VersionedAPIRouter()

    @router.get("/annotated")
    async def annotated(value: Annotated[str, Depends(dependency)]):
        return value

    @router.get("/default")
    async def default(value: str = Depends(dependency)):
        return value

    @router.get("/route", dependencies=[Depends(dependency)])
    async def route_level():
        return "overridden"

    app = Cadwyn(versions=VersionBundle(HeadVersion(), *versions(version_change(), version_change())))
    if override_before_generation:
        app.dependency_overrides[dependency] = lambda: "overridden"
    app.generate_and_include_versioned_routers(router)
    if not override_before_generation:
        app.dependency_overrides[dependency] = lambda: "overridden"

    with TestClient(app) as client:
        for version in ("2002-01-01", "2001-01-01", "2000-01-01"):
            for path in ("/annotated", "/default", "/route"):
                response = client.get(path, headers={"x-api-version": version})
                assert response.json() == "overridden"


def test__unversioned_include_router__route_added_after_inclusion_is_available():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    parent_router = APIRouter(prefix="/api")
    child_router = APIRouter()

    def mark_parent_router_dependency(response: Response):
        response.headers["x-parent-router-dependency"] = "ran"

    parent_router.include_router(
        child_router,
        prefix="/health",
        dependencies=[Depends(mark_parent_router_dependency)],
    )
    app.include_router(parent_router)

    @child_router.get("")
    async def healthcheck():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200, response.json()
    assert response.json() == {"ok": True}
    assert response.headers["x-parent-router-dependency"] == "ran"


def test__unversioned_include_router__route_added_after_first_request_is_available():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    parent_router = APIRouter(prefix="/api")
    child_router = APIRouter()
    parent_router.include_router(child_router, prefix="/late")
    app.include_router(parent_router)

    with TestClient(app) as client:
        response = client.get("/api/late/route")
        assert response.status_code == 404, response.json()

        @child_router.get("/route", name="late_route")
        async def late_route():
            return {"late": True}

        assert app.url_path_for("late_route") == "/api/late/route"

        response = client.get("/api/late/route")

    assert response.status_code == 200, response.json()
    assert response.json() == {"late": True}


def test__unversioned_include_router__hidden_late_route_does_not_create_public_openapi_schema():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")))
    parent_router = APIRouter()
    child_router = APIRouter()
    parent_router.include_router(child_router, prefix="/internal", include_in_schema=False)
    app.include_router(parent_router)

    @child_router.get("/health")
    async def healthcheck():
        return {"ok": True}

    with TestClient(app) as client:
        route_response = client.get("/internal/health")
        schema_response = client.get("/openapi.json?version=unversioned")
        docs_response = client.get("/docs")

    assert route_response.status_code == 200, route_response.json()
    assert route_response.json() == {"ok": True}
    assert schema_response.status_code == 404, schema_response.json()
    assert "version=unversioned" not in docs_response.text


def test__unversioned_include_router__hidden_plain_route_does_not_create_public_openapi_schema():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")))
    router = APIRouter()

    async def hidden_route(_request):
        return Response("hidden")

    router.add_route("/internal/health", hidden_route, methods=["GET"], include_in_schema=False)
    app.include_router(router)

    with TestClient(app) as client:
        route_response = client.get("/internal/health")
        schema_response = client.get("/openapi.json?version=unversioned")
        docs_response = client.get("/docs")

    assert route_response.status_code == 200, route_response.text
    assert route_response.text == "hidden"
    assert schema_response.status_code == 404, schema_response.json()
    assert "version=unversioned" not in docs_response.text


def test__unversioned_include_router__visible_nested_plain_route_creates_public_openapi_schema():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")))
    parent_router = APIRouter()
    child_router = APIRouter()
    parent_router.include_router(child_router, prefix="/public")
    app.include_router(parent_router)

    async def public_route(_request):
        return Response("public")

    child_router.add_route("/health", public_route, methods=["GET"])

    with TestClient(app) as client:
        route_response = client.get("/public/health")
        schema_response = client.get("/openapi.json?version=unversioned")
        docs_response = client.get("/docs")

    assert route_response.status_code == 200, route_response.text
    assert route_response.text == "public"
    assert schema_response.status_code == 200, schema_response.json()
    assert "version=unversioned" in docs_response.text


def test__unversioned_include_router__included_tags_are_used_for_openapi_tag_filtering():
    app = Cadwyn(
        versions=VersionBundle(Version("2022-11-16")),
        openapi_tags=[
            {"name": "public", "description": "Public operations"},
            {"name": "private", "description": "Private operations"},
        ],
    )
    parent_router = APIRouter(tags=["private"])
    child_router = APIRouter()
    parent_router.include_router(child_router, prefix="/settings", tags=["public"])
    app.include_router(parent_router)

    @child_router.get("")
    async def get_settings():
        return {"settings": True}

    with TestClient(app) as client:
        route_response = client.get("/settings")
        response = client.get("/openapi.json?version=unversioned")

    assert route_response.status_code == 200, route_response.json()
    assert route_response.json() == {"settings": True}
    assert response.status_code == 200, response.json()
    tag_names = {tag["name"] for tag in response.json()["tags"]}
    assert tag_names == {"public", "private"}


def test__versioned_include_router__late_route_uses_dependency_overrides():
    app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2022-11-16")))
    parent_router = VersionedAPIRouter(prefix="/api")
    child_router = VersionedAPIRouter()

    async def old_dependency():
        raise NotImplementedError

    async def new_dependency():
        return "new"

    parent_router.include_router(child_router, prefix="/deps")
    app.generate_and_include_versioned_routers(parent_router)

    @child_router.get("")
    async def read_dependency_value(dependency: Annotated[str, Depends(old_dependency)]):
        return dependency

    app.dependency_overrides[old_dependency] = new_dependency

    with TestClient(app) as client:
        response = client.get("/api/deps", headers={"x-api-version": "2022-11-16"})

    assert response.status_code == 200, response.json()
    assert response.json() == "new"


def test__default_version__unversioned_included_route_added_late_still_has_priority():
    app = Cadwyn(
        versions=VersionBundle(HeadVersion(), Version("2022-11-16")),
        api_version_default_value="2022-11-16",
    )
    parent_router = APIRouter(prefix="/api")
    child_router = APIRouter()
    parent_router.include_router(child_router, prefix="/items")
    app.include_router(parent_router)

    versioned_router = VersionedAPIRouter(prefix="/api")

    @versioned_router.get("/items")
    async def versioned_items():
        return {"source": "versioned"}

    app.generate_and_include_versioned_routers(versioned_router)

    @child_router.get("")
    async def unversioned_items():
        return {"source": "unversioned"}

    with TestClient(app) as client:
        default_response = client.get("/api/items")
        versioned_response = client.get("/api/items", headers={"x-api-version": "2022-11-16"})

    assert default_response.status_code == 200, default_response.json()
    assert default_response.json() == {"source": "unversioned"}
    assert versioned_response.status_code == 200, versioned_response.json()
    assert versioned_response.json() == {"source": "versioned"}


@pytest.mark.parametrize("router_location", ["versioned", "versioned_parent", "unversioned"])
@pytest.mark.parametrize(
    ("app_kwargs", "router_kwargs", "route_kwargs", "expected_status_code"),
    [
        ({"strict_content_type": False}, {}, {}, 200),
        ({"strict_content_type": False}, {"strict_content_type": True}, {}, 422),
        ({"strict_content_type": False}, {}, {"strict_content_type": True}, 422),
        ({}, {"strict_content_type": False}, {}, 200),
        ({}, {}, {"strict_content_type": False}, 200),
    ],
)
def test__strict_content_type__should_propagate_from_app_and_routers_to_routes_like_in_fastapi(
    router_location: Literal["versioned", "versioned_parent", "unversioned"],
    app_kwargs: dict[str, Any],
    router_kwargs: dict[str, Any],
    route_kwargs: dict[str, Any],
    expected_status_code: int,
):
    class Item(BaseModel):
        a: int

    def make_router() -> APIRouter:
        if router_location == "versioned_parent":
            router = VersionedAPIRouter(**router_kwargs)
            child_router = VersionedAPIRouter()
            router.include_router(child_router)
        else:
            router = child_router = VersionedAPIRouter(**router_kwargs)

        async def create_item(item: Item):
            return item

        # FastAPI's route decorators don't accept strict_content_type
        child_router.add_api_route("/items", create_item, methods=["POST"], **route_kwargs)
        return router

    fastapi_app = FastAPI(**app_kwargs)
    fastapi_app.include_router(make_router())
    cadwyn_app = Cadwyn(versions=VersionBundle(Version("2022-11-16")), **app_kwargs)
    if router_location == "unversioned":
        cadwyn_app.include_router(make_router())
        headers = {}
    else:
        cadwyn_app.generate_and_include_versioned_routers(make_router())
        headers = {"x-api-version": "2022-11-16"}

    # No Content-Type header
    with TestClient(fastapi_app) as fastapi_client, TestClient(cadwyn_app) as cadwyn_client:
        fastapi_response = fastapi_client.post("/items", content=b'{"a": 1}')
        cadwyn_response = cadwyn_client.post("/items", content=b'{"a": 1}', headers=headers)

    assert fastapi_response.status_code == expected_status_code, fastapi_response.json()
    assert cadwyn_response.status_code == expected_status_code, cadwyn_response.json()
    if expected_status_code == 200:
        assert cadwyn_response.json() == {"a": 1}


def test__header_routing_fastapi_add_header_versioned_routers__apirouter_is_empty__version_should_not_have_any_routes():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # This test verifies the legacy API.
            APIRouter(),
            header_value="2022-11-16",
        )
    assert len(app.router.versioned_routers) == 1
    assert len(app.router.versioned_routers["2022-11-16"].routes) == 1
    route = cast("APIRoute", app.router.versioned_routers["2022-11-16"].routes[0])
    assert route.path == "/openapi.json"


@pytest.mark.parametrize("client", [client_without_headers, client_without_headers_and_with_custom_api_version_var])
def test__header_based_versioning(client: TestClient):
    resp = client.get("/v1", headers=BASIC_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"my_version1": 1}
    assert resp.headers["X-API-VERSION"] == "2021-01-01"

    resp = client.get("/v1", headers=BASIC_HEADERS | {"X-API-VERSION": "2022-02-02"})
    assert resp.status_code == 200
    assert resp.json() == {"my_version2": 2}
    assert resp.headers["X-API-VERSION"] == "2022-02-02"

    resp = client.get("/v1", headers=BASIC_HEADERS | {"X-API-VERSION": "2024-02-02"})
    assert resp.status_code == 200
    assert resp.json() == {"my_version2": 2}
    assert resp.headers["X-API-VERSION"] == "2022-02-02"


def test__header_based_versioning__invalid_version_header_format__should_raise_422():
    resp = client_without_headers.get("/v1", headers=BASIC_HEADERS | {"X-API-VERSION": "2022-02_02"})
    assert resp.status_code == 422
    assert resp.json() == {
        "detail": [
            {
                "type": "date_from_datetime_parsing",
                "loc": ["header", "x-api-version"],
                "msg": "Input should be a valid date or datetime, invalid date separator, expected `-`",
                "input": "2022-02_02",
                "ctx": {"error": "invalid date separator, expected `-`"},
            }
        ]
    }


def test__get_unversioned_router():
    resp = client_without_headers.post("/v1/unversioned")
    assert resp.status_code == 200
    assert resp.json() == {"saved": True}


def test__get_openapi():
    resp = client_without_headers.get("/openapi.json", headers={"x-api-version": "2021-01-01"})
    assert resp.status_code == 200

    resp = client_without_headers.get("/openapi.json?version=2021-01-01")
    assert resp.status_code == 200


def test__get_openapi__api_version_header_has_configured_metadata_and_date_schema():
    app = Cadwyn(
        versions=VersionBundle(Version("2022-11-16")),
        api_version_default_value="2022-11-16",
        api_version_title="Requested API version",
        api_version_description="Selects the version contract",
    )
    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items():
        return []

    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        items_response = client.get("/items")
        response = client.get("/openapi.json?version=2022-11-16")

    assert items_response.status_code == 200
    assert items_response.json() == []
    assert response.status_code == 200, response.json()
    assert response.json()["paths"]["/items"]["get"]["parameters"] == [
        {
            "name": "x-api-version",
            "in": "header",
            "required": False,
            "schema": {
                "type": "string",
                "format": "date",
                "title": "Requested API version",
                "description": "Selects the version contract",
                "default": "2022-11-16",
            },
            "description": "Selects the version contract",
            "examples": {"default": {"value": "2022-11-16"}},
        }
    ]


async def _get_default_api_version(request: Request) -> str:
    return "2023-04-14"


def _get_api_version_parameters_from_openapi(app: Cadwyn, path: str) -> dict[str, list[dict[str, Any]]]:
    with TestClient(app) as client:
        return {
            version: client.get(f"/openapi.json?version={version}").json()["paths"][path]["get"]["parameters"]
            for version in app.router.versions
        }


@pytest.mark.parametrize(
    ("api_version_format", "version_values", "expected_schema"),
    [
        ("date", ("2023-04-14", "2022-11-16"), {"type": "string", "format": "date", "title": "X-Api-Version"}),
        ("string", ("v2", "v1"), {"type": "string", "title": "X-Api-Version"}),
    ],
)
def test__get_openapi__api_version_header_without_default_value__should_be_required(
    api_version_format: Literal["date", "string"], version_values: tuple[str, str], expected_schema: dict[str, Any]
):
    app = Cadwyn(
        versions=VersionBundle(*(Version(version) for version in version_values)),
        api_version_format=api_version_format,
    )
    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items():
        return []

    app.generate_and_include_versioned_routers(router)

    assert _get_api_version_parameters_from_openapi(app, "/items") == {
        version: [
            {
                "name": "x-api-version",
                "in": "header",
                "required": True,
                "schema": expected_schema | {"default": version},
                "examples": {"default": {"value": version}},
            }
        ]
        for version in version_values
    }
    with TestClient(app) as client:
        assert client.get("/items").status_code == 404
        assert client.get("/items", headers={"x-api-version": version_values[1]}).json() == []


@pytest.mark.parametrize("api_version_default_value", ["2023-04-14", _get_default_api_version])
def test__get_openapi__api_version_header_with_default_value__should_stay_optional(
    api_version_default_value: "str | Callable[[Request], Awaitable[str]]",
):
    app = Cadwyn(
        versions=VersionBundle(Version("2023-04-14"), Version("2022-11-16")),
        api_version_default_value=api_version_default_value,
    )
    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items(request: Request):
        return request.scope["cadwyn.api_version"]

    app.generate_and_include_versioned_routers(router)

    assert _get_api_version_parameters_from_openapi(app, "/items") == {
        version: [
            {
                "name": "x-api-version",
                "in": "header",
                "required": False,
                "schema": {"type": "string", "format": "date", "default": version, "title": "X-Api-Version"},
                "examples": {"default": {"value": version}},
            }
        ]
        for version in ("2023-04-14", "2022-11-16")
    }
    with TestClient(app) as client:
        assert client.get("/items").json() == "2023-04-14"
        assert client.get("/items", headers={"x-api-version": "2022-11-16"}).json() == "2022-11-16"


def test__get_openapi__api_version_in_path__should_stay_required_without_default():
    app = Cadwyn(
        versions=VersionBundle(Version("v2"), Version("v1")),
        api_version_location="path",
        api_version_parameter_name="api_version",
        api_version_format="string",
    )
    router = VersionedAPIRouter(prefix="/{api_version}")

    @router.get("/items")
    async def get_items():
        return []

    app.generate_and_include_versioned_routers(router)

    assert _get_api_version_parameters_from_openapi(app, "/{api_version}/items") == {
        version: [
            {
                "name": "api_version",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "title": "Api Version"},
                "examples": {"default": {"value": version}},
            }
        ]
        for version in ("v2", "v1")
    }
    with TestClient(app) as client:
        assert client.get("/v1/items").json() == []


def test__get_openapi__route_with_ref_parameters__should_keep_them_and_require_api_version_header():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    router = VersionedAPIRouter()

    @router.get("/items", openapi_extra={"parameters": [{"$ref": "#/components/parameters/TenantId"}]})
    async def get_items():
        raise NotImplementedError

    app.generate_and_include_versioned_routers(router)

    assert _get_api_version_parameters_from_openapi(app, "/items") == {
        "2022-11-16": [
            {
                "name": "x-api-version",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "format": "date", "default": "2022-11-16", "title": "X-Api-Version"},
                "examples": {"default": {"value": "2022-11-16"}},
            },
            {"$ref": "#/components/parameters/TenantId"},
        ]
    }


_OWN_HEADER_WITH_API_VERSION_NAME = {
    "name": "x-api-version",
    "in": "header",
    "required": False,
    "schema": {"type": "string", "default": "legacy-client", "title": "X-Api-Version"},
}


def test__get_openapi__unversioned_route_with_own_header_named_like_api_version__should_stay_optional():
    app = Cadwyn(versions=VersionBundle(Version("2023-04-14")))

    @app.get("/health")
    async def health(client_version: str = Header("legacy-client", alias="x-api-version")):
        return client_version

    with TestClient(app) as client:
        response = client.get("/health")
        openapi = client.get("/openapi.json?version=unversioned").json()

    assert response.json() == "legacy-client"
    assert openapi["paths"]["/health"]["get"]["parameters"] == [_OWN_HEADER_WITH_API_VERSION_NAME]


def test__get_openapi__api_version_in_path_with_own_header_named_like_api_version__should_stay_optional():
    app = Cadwyn(versions=VersionBundle(Version("2023-04-14")), api_version_location="path")
    router = VersionedAPIRouter()

    @router.get("/{x_api_version}/items")
    async def get_items(client_version: str = Header("legacy-client", alias="x-api-version")):
        return client_version

    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        response = client.get("/2023-04-14/items")

    assert response.json() == "legacy-client"
    assert _get_api_version_parameters_from_openapi(app, "/{x_api_version}/items") == {
        "2023-04-14": [
            {
                "name": "x_api_version",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "format": "date", "title": "X Api Version"},
                "examples": {"default": {"value": "2023-04-14"}},
            },
            _OWN_HEADER_WITH_API_VERSION_NAME,
        ]
    }


def test__api_version_header_without_default_value__version_picked_by_custom_middleware__should_not_require_header():
    class QueryVersionPickingMiddleware(VersionPickingMiddleware):
        @override
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
            self.api_version_var.set(request.query_params.get("version"))
            return await call_next(request)

    app = Cadwyn(
        versions=VersionBundle(Version("2023-04-14"), Version("2022-11-16")),
        versioning_middleware_class=QueryVersionPickingMiddleware,
    )
    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items(request: Request):
        return request.scope["cadwyn.api_version"]

    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        response = client.get("/items?version=2022-11-16")

    assert response.status_code == 200, response.json()
    assert response.json() == "2022-11-16"


def test__get_openapi__nonexisting_version__should_return_404():
    resp = client_without_headers.get("/openapi.json?version=2023-01-01")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "OpenApi file with version `2023-01-01` not found"}


def test__get_openapi__with_mounted_app__should_include_root_path_in_servers():
    root_app = FastAPI()
    root_app.mount("/my_api", Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16"))))
    client = TestClient(root_app)

    resp = client.get("/my_api/openapi.json?version=2022-11-16")
    servers = resp.json()["servers"]
    assert "/my_api" in [server["url"] for server in servers]


@pytest.mark.skipif(not FASTAPI_ACCEPTS_OPENAPI_PREFIX, reason="FastAPI 0.145.0 removed openapi_prefix")
def test__get_openapi__with_openapi_prefix__should_warn_and_include_it_in_servers():
    with pytest.warns(DeprecationWarning, match=re.escape("`openapi_prefix` is deprecated")):
        app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")), openapi_prefix="/my_api")
    client = TestClient(app)

    resp = client.get("/openapi.json?version=2022-11-16")
    assert resp.json()["servers"] == [{"url": "/my_api"}]


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.145
    FASTAPI_ACCEPTS_OPENAPI_PREFIX, reason="FastAPI accepts openapi_prefix before 0.145.0"
)
def test__cadwyn__with_openapi_prefix_on_fastapi_without_it__should_raise_a_clear_error():
    with pytest.raises(TypeError, match=re.escape("FastAPI 0.145.0 removed it")):
        Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")), openapi_prefix="/my_api")


def test__get_docs__without_unversioned_routes__should_return_all_versioned_doc_urls():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")))
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2021_01_01_router,
            header_value="2021-01-01",
        )
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2022_01_02_router,
            header_value="2022-02-02",
        )

    client = TestClient(app)

    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "http://testserver/docs?version=2021-01-01" in resp.text
    assert "http://testserver/docs?version=2022-02-02" in resp.text
    assert "http://testserver/docs?version=unversioned" not in resp.text

    resp = client.get("/redoc")
    assert resp.status_code == 200
    assert "http://testserver/redoc?version=2021-01-01" in resp.text
    assert "http://testserver/redoc?version=2022-02-02" in resp.text
    assert "http://testserver/redoc?version=unversioned" not in resp.text


@pytest.mark.parametrize("docs_url", ["/docs", "/redoc"])
@pytest.mark.parametrize(
    ("server_root_path", "mount_path"),
    [
        pytest.param("", "/my_api", id="mounted"),
        pytest.param("/prefix", "", id="server_root_path"),
        pytest.param("/prefix", "/my_api", id="server_root_path_and_mounted"),
    ],
)
def test__get_docs__with_root_path__should_link_to_reachable_versioned_docs(
    server_root_path: str, mount_path: str, docs_url: str
):
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version("2022-11-16")))
    if mount_path:
        root_app = FastAPI()
        root_app.mount(mount_path, app)
    else:
        root_app = app
    client = TestClient(root_app, root_path=server_root_path)
    app_url = f"http://testserver{server_root_path}{mount_path}"

    resp = client.get(f"{app_url}{docs_url}")

    assert resp.status_code == 200
    assert re.findall(r'href="(http://testserver[^"]*)"', resp.text) == [f"{app_url}{docs_url}?version=2022-11-16"]
    versioned_docs = client.get(f"{app_url}{docs_url}?version=2022-11-16")
    assert versioned_docs.status_code == 200
    assert f"{server_root_path}{mount_path}/openapi.json?version=2022-11-16" in versioned_docs.text


def test__mount__static_files__should_serve_file(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "hello.txt").write_text("Hello World")

    app = Cadwyn(changelog_url=None, versions=VersionBundle(HeadVersion(), Version("2022-11-16")))
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    with TestClient(app) as client:
        resp = client.get("/static/hello.txt")

    assert resp.status_code == 200
    assert resp.text == "Hello World"


def test__get_docs__with_unversioned_routes__should_return_all_versioned_doc_urls():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2021_01_01_router,
            header_value="2021-01-01",
        )
    with pytest.warns(DeprecationWarning, match=_ADD_HEADER_WARNING):
        app.add_header_versioned_routers(  # ty: ignore[deprecated]  # Legacy test setup.
            v2022_01_02_router,
            header_value="2022-02-02",
        )

    @app.post("/my_unversioned_route")
    def my_unversioned_route():
        raise NotImplementedError

    client = TestClient(app)

    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "http://testserver/docs?version=2022-02-02" in resp.text
    assert "http://testserver/docs?version=2021-01-01" in resp.text
    assert "http://testserver/docs?version=unversioned" in resp.text

    resp = client.get("/redoc")
    assert resp.status_code == 200
    assert "http://testserver/redoc?version=2022-02-02" in resp.text
    assert "http://testserver/redoc?version=2021-01-01" in resp.text
    assert "http://testserver/redoc?version=unversioned" in resp.text


# I wish we could check it properly but it's a dynamic page and I'm not in the mood of adding selenium
def test__get_docs__specific_version():
    resp = client_without_headers.get("/docs?version=2022-01-01")
    assert resp.status_code == 200

    resp = client_without_headers.get("/redoc?version=2022-01-01")
    assert resp.status_code == 200


def test__get_unversioned_with_redirect():
    resp = client_without_headers.post("/v1/unversioned/")
    assert resp.status_code == 200
    assert resp.json() == {"saved": True}


def test__get_unversioned_as_partial_because_of_method():
    resp = client_without_headers.patch("/v1/unversioned")
    assert resp.status_code == 405


def test__empty_root():
    resp = client_without_headers.get("/")
    assert resp.status_code == 404


def test__background_tasks():
    background_task_data = None

    def my_background_task(email: str, message: str):
        nonlocal background_task_data
        background_task_data = (email, message)

    router = VersionedAPIRouter()

    @router.post("/send-notification/{email}")
    async def send_notification(email: str, background_tasks: BackgroundTasks):
        background_tasks.add_task(my_background_task, email, message="some notification")
        return {"message": "Notification sent in the background"}

    app = Cadwyn(versions=VersionBundle(HeadVersion(), Version(DEFAULT_API_VERSION)))
    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        resp = client.post("/send-notification/test@example.com", headers=BASIC_HEADERS)
        assert resp.status_code == 200, resp.json()
        assert background_task_data == ("test@example.com", "some notification")


def test__webhooks():
    webhooks = VersionedAPIRouter()

    class Subscription(BaseModel):
        username: str
        monthly_fee: float
        start_date: str

    @webhooks.post("new-subscription")
    def new_subscription(body: Subscription):  # pragma: no cover
        """We'll send you a POST request with this data upon new user subscription"""

    class MyVersionChange(VersionChange):
        description = "Mess with webhooks"
        instructions_to_migrate_to_previous_version = [
            endpoint("new-subscription", ["POST"]).didnt_exist,
            schema(Subscription).field("monthly_fee").didnt_exist,
        ]

    app = Cadwyn(
        versions=VersionBundle(HeadVersion(), Version("2023-04-12", MyVersionChange), Version("2022-11-16")),
        webhooks=webhooks,
    )

    @app.webhooks.post("post-subscription")  # pragma: no cover
    def post_subscription(body: Subscription):  # pragma: no cover
        """I should also appear there"""

    with TestClient(app) as client:
        resp = client.get("/openapi.json?version=2023-04-12")
        openapi_dict = resp.json()

        assert "webhooks" in openapi_dict, "'webhooks' section is missing"
        assert "new-subscription" in openapi_dict["webhooks"], "'new-subscription' webhook is missing"
        assert "post-subscription" in openapi_dict["webhooks"], "'post-subscription' webhook is missing"
        assert "post" in openapi_dict["webhooks"]["post-subscription"], "POST method for 'post-subscription' is missing"
        assert "Subscription" in openapi_dict["components"]["schemas"], "'Subscription' component is missing"
        assert "monthly_fee" in openapi_dict["components"]["schemas"]["Subscription"]["properties"], (
            "monthly_fee field is missing"
        )

        resp = client.get("/openapi.json?version=2022-11-16")
        openapi_dict = resp.json()

        assert "webhooks" in openapi_dict, "'webhooks' section is missing"
        assert "new-subscription" not in openapi_dict["webhooks"], "'new-subscription' webhook is missing"
        assert "post-subscription" in openapi_dict["webhooks"], "'post-subscription' webhook is present"
        assert "post" in openapi_dict["webhooks"]["post-subscription"], "POST method for 'post-subscription' is missing"
        assert "Subscription" in openapi_dict["components"]["schemas"], "'Subscription' component is missing"
        assert "monthly_fee" not in openapi_dict["components"]["schemas"]["Subscription"]["properties"], (
            "monthly_fee field is present yet it must be deleted"
        )


def test__docs_dashboards__custom_static_asset_urls():
    app = Cadwyn(
        versions=VersionBundle(Version("2022-11-16")),
        changelog_url=None,
        swagger_js_url="/static/swagger.js",
        swagger_css_url="/static/swagger.css",
        swagger_favicon_url="/static/swagger-favicon.png",
        redoc_js_url="/static/redoc.js",
        redoc_favicon_url="/static/redoc-favicon.png",
    )
    with TestClient(app) as client:
        swagger_resp = client.get("/docs?version=2022-11-16")
        assert swagger_resp.status_code == 200
        assert "/static/swagger.js" in swagger_resp.text
        assert "/static/swagger.css" in swagger_resp.text
        assert "/static/swagger-favicon.png" in swagger_resp.text

        redoc_resp = client.get("/redoc?version=2022-11-16")
        assert redoc_resp.status_code == 200
        assert "/static/redoc.js" in redoc_resp.text
        assert "/static/redoc-favicon.png" in redoc_resp.text


def test__api_version_header_name_is_deprecated_and_translates_to_api_version_parameter_name():
    with pytest.warns(
        DeprecationWarning,
        match="api_version_header_name is deprecated",
    ):
        cadwyn = Cadwyn(api_version_header_name="x-api-version", versions=VersionBundle(Version("2022-11-16")))
    assert cadwyn.api_version_parameter_name == "x-api-version"


def test__openapi_tags__unversioned_should_only_include_tags_used_by_routes():
    app = Cadwyn(
        versions=VersionBundle(Version("2022-11-16")),
        openapi_tags=[
            {"name": "users", "description": "User operations"},
            {"name": "settings", "description": "Settings operations"},
        ],
    )

    versioned_router = VersionedAPIRouter()

    @versioned_router.get("/users", tags=["users"])
    def get_users():
        raise NotImplementedError

    app.generate_and_include_versioned_routers(versioned_router)

    @app.post("/my_settings", tags=["settings"])
    def my_settings():
        raise NotImplementedError

    with TestClient(app) as client:
        # Versioned schema should only include "users" tag (the only tag used by versioned routes)
        resp = client.get("/openapi.json?version=2022-11-16")
        versioned_tags = resp.json().get("tags", [])
        versioned_tag_names = [t["name"] for t in versioned_tags]
        assert "users" in versioned_tag_names
        assert "settings" not in versioned_tag_names

        # Unversioned schema should only include "settings" tag (the only tag used by unversioned routes)
        resp = client.get("/openapi.json?version=unversioned")
        unversioned_tags = resp.json().get("tags", [])
        unversioned_tag_names = [t["name"] for t in unversioned_tags]
        assert "settings" in unversioned_tag_names
        assert "users" not in unversioned_tag_names


class _LifespanSchema(BaseModel):
    name: str
    monthly_fee: float


def _multi_version_bundle():
    class RemoveMonthlyFee(VersionChange):
        description = "Remove monthly_fee from the schema in the older version"
        instructions_to_migrate_to_previous_version = [schema(_LifespanSchema).field("monthly_fee").didnt_exist]

    return VersionBundle(
        HeadVersion(),
        Version("2023-04-12", RemoveMonthlyFee),
        Version("2022-11-16"),
    )


def test__lifespan__should_be_entered_exactly_once_per_startup():
    entered = 0
    exited = 0

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal entered, exited
        entered += 1
        try:
            yield
        finally:
            exited += 1

    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items() -> _LifespanSchema:  # pragma: no cover
        return _LifespanSchema(name="a", monthly_fee=1.0)

    app = Cadwyn(versions=_multi_version_bundle(), lifespan=lifespan)
    app.generate_and_include_versioned_routers(router)

    with TestClient(app):
        assert entered == 1
    assert entered == 1
    assert exited == 1


def test__on_startup_and_on_shutdown__should_run_exactly_once_per_startup():
    startups = 0
    shutdowns = 0

    def on_startup():
        nonlocal startups
        startups += 1

    def on_shutdown():
        nonlocal shutdowns
        shutdowns += 1

    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items() -> _LifespanSchema:  # pragma: no cover
        return _LifespanSchema(name="a", monthly_fee=1.0)

    app = Cadwyn(
        versions=_multi_version_bundle(),
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
    )
    app.generate_and_include_versioned_routers(router)

    with TestClient(app):
        assert startups == 1
    assert startups == 1
    assert shutdowns == 1


def test__api_version_default_value_with_path_location__should_raise_error():
    with pytest.raises(
        CadwynStructureError,
        match="You tried to pass an api_version_default_value while putting the API version in Path",
    ):
        Cadwyn(
            versions=VersionBundle(HeadVersion(), Version("2022-11-16")),
            api_version_default_value="2022-11-16",
            api_version_location="path",
        )


# The pinned uvicorn calls `asyncio.iscoroutinefunction` while loading the app, which is deprecated on Python 3.14
@pytest.mark.filterwarnings("ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning")
def test__cadwyn__loaded_by_uvicorn__should_not_be_mistaken_for_an_app_factory():
    import uvicorn

    app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))
    config = uvicorn.Config(app, proxy_headers=False, ws="none")
    config.load()

    assert config.loaded_app is app


def test__cadwyn__reassigned_dependency_overrides__should_be_applied_in_all_versions():
    def dependency() -> str:
        raise NotImplementedError

    router = VersionedAPIRouter()

    @router.get("/versioned")
    async def versioned(value: Annotated[str, Depends(dependency)]):
        return value

    app = Cadwyn(versions=VersionBundle(HeadVersion(), *versions(version_change())))

    @app.get("/unversioned")
    async def unversioned(value: Annotated[str, Depends(dependency)]):
        return value

    app.generate_and_include_versioned_routers(router)
    app.dependency_overrides[dependency] = lambda: "initial"

    with TestClient(app) as client:
        # Cadwyn generates the versioned routers on the first request
        assert client.get("/versioned", headers={"x-api-version": "2000-01-01"}).json() == "initial"
        app.dependency_overrides = {dependency: lambda: "overridden"}

        assert client.get("/unversioned").json() == "overridden"
        for version in ("2001-01-01", "2000-01-01"):
            assert client.get("/versioned", headers={"x-api-version": version}).json() == "overridden"


def test__cadwyn_router__accessed_on_the_class__should_not_break_introspection():
    app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))

    assert isinstance(app.router, _RootCadwynAPIRouter)
    assert dict(inspect.getmembers(Cadwyn))["router"] is not None


def test__cadwyn_router__before_init__should_be_missing():
    app = Cadwyn.__new__(Cadwyn)

    assert not hasattr(app, "router")
    with pytest.raises(AttributeError, match="router"):
        del app.router


def test__cadwyn_router__deleted__should_be_missing():
    app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))

    del app.router

    assert not hasattr(app, "router")
