import asyncio
import functools
import inspect
import logging
import re
import threading
import warnings
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Literal, cast

import httpx
import pytest
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from typing_extensions import override

from cadwyn import Cadwyn, current_dependency_solver
from cadwyn.exceptions import CadwynStructureError
from cadwyn.middleware import VersionPickingMiddleware
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.routing import _RootCadwynAPIRouter
from cadwyn.structure.endpoints import endpoint
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import HeadVersion, Version, VersionBundle, VersionChange
from tests._resources.utils import BASIC_HEADERS, DEFAULT_API_VERSION
from tests._resources.versioned_app.app import (
    client_without_headers,
    client_without_headers_and_with_custom_api_version_var,
    versioned_router,
)
from tests.conftest import version_change, versions


def _two_versions() -> VersionBundle:
    return VersionBundle(Version("2022-02-02"), Version("2021-01-01"))


def test__header_routing_fastapi_init__openapi_passing_nulls__should_not_add_openapi_routes():
    assert [cast("APIRoute", r).path for r in Cadwyn(versions=VersionBundle(Version("2022-11-16"))).routes] == [
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/docs",
        "/redoc",
    ]
    assert [
        cast("APIRoute", r).path
        for r in Cadwyn(versions=VersionBundle(Version("2022-11-16")), docs_url=None, redoc_url=None).routes
    ] == [
        "/openapi.json",
    ]
    assert Cadwyn(versions=VersionBundle(Version("2022-11-16")), openapi_url=None).routes == []


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("changelog_url", "/changelog"),
        ("changelog_url", ""),
        ("include_changelog_url_in_schema", True),
        ("include_changelog_url_in_schema", 0),
    ],
)
def test__cadwyn_init__removed_changelog_argument__should_raise_type_error(argument: str, value: Any):
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{argument}'.*removed the changelog feature"):
        Cadwyn(versions=VersionBundle(Version("2022-11-16")), **{argument: value})


@pytest.mark.parametrize(("argument", "value"), [("changelog_url", None), ("include_changelog_url_in_schema", False)])
def test__cadwyn_init__removed_changelog_argument_that_turns_it_off__should_warn_and_do_nothing(
    argument: str, value: Any
):
    with pytest.warns(DeprecationWarning, match=f"`{argument}={value!r}` does nothing"):
        app = Cadwyn(versions=VersionBundle(Version("2022-11-16")), **{argument: value})
    # FastAPI 0.162 dropped `**extra` and `app.extra`; before that, unknown arguments ended up there
    assert argument not in getattr(app, "extra", {})
    assert "/changelog" not in [cast("APIRoute", route).path for route in app.routes]


def test__header_routing_fastapi_init__passing_null_to_oauth2__should_not_add_oauth2_redirect_route():
    app = Cadwyn(versions=VersionBundle(Version("2021-01-01")), swagger_ui_oauth2_redirect_url=None)
    assert [cast("APIRoute", r).path for r in app.routes] == [
        "/openapi.json",
        "/docs",
        "/redoc",
    ]
    app.generate_and_include_versioned_routers(versioned_router)

    with TestClient(app) as client:
        assert client.get("/docs?version=2021-01-01").status_code == 200


def test__header_routing_fastapi_init__changing_openapi_url__docs_still_return_200():
    app = Cadwyn(versions=_two_versions(), openapi_url="/openpapi")
    app.generate_and_include_versioned_routers(versioned_router)
    with TestClient(app) as client:
        assert client.get("/openpapi?version=2021-01-01").status_code == 200
        assert client.get("/openapi.json?version=2021-01-01").status_code == 404


def test__header_routing_fastapi__calling_openapi_incorrectly__docs_should_return_404():
    app = Cadwyn(versions=_two_versions())
    app.generate_and_include_versioned_routers(versioned_router)
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

    cadwyn_router = VersionedAPIRouter()

    @cadwyn_router.post("/my_old_friend")
    async def my_old_friend(dependency: Annotated[str, Depends(old_dependency)]):
        return dependency

    app.generate_and_include_versioned_routers(regular_router, cadwyn_router)

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
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
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
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
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
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
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


def test__generate_and_include_versioned_routers__apirouter_is_empty__version_should_only_have_openapi_route():
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
    app.generate_and_include_versioned_routers(APIRouter())
    with TestClient(app):
        pass
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


@pytest.mark.parametrize("version", ["2022-02-02", "2021-01-01", "unversioned"])
def test__openapi_customizer__none__should_match_output_without_customizer(version: str):
    def make_app(**kwargs: Any) -> Cadwyn:
        app = Cadwyn(versions=_two_versions(), **kwargs)
        router = VersionedAPIRouter()

        @router.get("/items")
        def get_items() -> list[str]:
            raise NotImplementedError

        @app.get("/health")
        def health() -> str:
            raise NotImplementedError

        app.generate_and_include_versioned_routers(router)
        return app

    responses = []
    for app in (make_app(), make_app(openapi_customizer=None)):
        with TestClient(app, root_path="/proxy") as client:
            responses.append(client.get("/openapi.json", params={"version": version}))
    without_customizer, with_none = responses
    assert without_customizer.status_code == with_none.status_code == 200
    assert with_none.content == without_customizer.content


@pytest.mark.parametrize(("api_version_format", "version"), [("date", "2022-02-02"), ("string", "v2")])
@pytest.mark.parametrize("use_header", [True, False])
def test__openapi_customizer__version_selected__should_receive_version_and_final_header_schema(
    api_version_format, version: str, use_header: bool
):
    def customize(schema: dict[str, Any], selected_version: str | None) -> dict[str, Any]:
        schema["x-selected-version"] = selected_version
        # This would be undone if Cadwyn's required-header adjustment ran after the callback.
        parameter = schema["paths"]["/items"]["get"]["parameters"][0]
        schema["x-original-required"] = parameter["required"]
        parameter["required"] = False
        return schema

    app = Cadwyn(
        versions=VersionBundle(Version(version)), api_version_format=api_version_format, openapi_customizer=customize
    )
    router = VersionedAPIRouter()

    @router.get("/items")
    def get_items() -> list[str]:
        raise NotImplementedError

    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        response = client.get(
            "/openapi.json",
            headers={"x-api-version": version} if use_header else {},
            params={} if use_header else {"version": version},
        )
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["version"] == version
    assert schema["x-selected-version"] == version
    assert schema["x-original-required"] is True
    assert schema["paths"]["/items"]["get"]["parameters"][0]["required"] is False


def test__openapi_customizer__unversioned_document__should_receive_none():
    def customize(schema: dict[str, Any], version: str | None) -> dict[str, Any]:
        return {**schema, "x-selected-version": version}

    app = Cadwyn(versions=_two_versions(), openapi_customizer=customize)

    @app.get("/health")
    def health() -> str:
        raise NotImplementedError

    with TestClient(app) as client:
        response = client.get("/openapi.json?version=unversioned")
    assert response.status_code == 200
    assert response.json()["info"]["version"] == "unversioned"
    assert response.json()["x-selected-version"] is None


def test__openapi_customizer__replacement_dict__should_serialize_replacement_without_further_changes():
    def customize(schema: dict[str, Any], version: str | None) -> dict[str, Any]:
        return {"replacement": version}

    with TestClient(Cadwyn(versions=_two_versions(), openapi_customizer=customize)) as client:
        response = client.get("/openapi.json?version=2022-02-02")
    assert response.status_code == 200
    assert response.content == b'{"replacement":"2022-02-02"}'


@pytest.mark.parametrize("invalid_return", [None, [], "schema", 1])
def test__openapi_customizer__non_dict_return__should_raise_type_error_naming_callback(invalid_return):
    def invalid_customizer(schema: dict[str, Any], version: str | None):
        return invalid_return

    with (
        TestClient(Cadwyn(versions=_two_versions(), openapi_customizer=invalid_customizer)) as client,
        pytest.raises(TypeError, match=r"invalid_customizer.*must return a dict"),
    ):
        client.get("/openapi.json?version=2022-02-02")


async def _async_customizer(schema: dict[str, Any], version: str | None) -> dict[str, Any]:
    raise AssertionError("Async customizers must not be called")


class _AsyncCallableCustomizer:
    async def __call__(self, schema: dict[str, Any], version: str | None) -> dict[str, Any]:
        raise AssertionError("Async customizers must not be called")


@pytest.mark.parametrize(
    ("customizer", "name"),
    [
        (_async_customizer, "_async_customizer"),
        (functools.partial(_async_customizer), "_async_customizer"),
        (_AsyncCallableCustomizer(), "_AsyncCallableCustomizer"),
        (functools.partial(_AsyncCallableCustomizer()), "_AsyncCallableCustomizer"),
        (functools.partial(functools.partial(_AsyncCallableCustomizer())), "_AsyncCallableCustomizer"),
        (staticmethod(_async_customizer), "_async_customizer"),
        (classmethod(_async_customizer), "_async_customizer"),
        (functools.partial(staticmethod(_async_customizer)), "_async_customizer"),
        (staticmethod(functools.partial(_AsyncCallableCustomizer())), "_AsyncCallableCustomizer"),
    ],
)
def test__openapi_customizer__async_callback__should_raise_type_error_at_startup(customizer: Any, name: str):
    with pytest.raises(TypeError, match=rf"openapi_customizer must be synchronous.*{name}"):
        Cadwyn(versions=_two_versions(), openapi_customizer=customizer)


def test__openapi_customizer__callback_raises__should_propagate_exception():
    error = ValueError("could not customize schema")

    def customize(schema: dict[str, Any], version: str | None) -> dict[str, Any]:
        raise error

    with (
        TestClient(Cadwyn(versions=_two_versions(), openapi_customizer=customize)) as client,
        pytest.raises(ValueError, match="could not customize schema") as caught,
    ):
        client.get("/openapi.json?version=2022-02-02")
    assert caught.value is error


@pytest.mark.parametrize(
    ("url", "status_code"),
    [
        ("/docs", 200),
        ("/redoc?version=2022-02-02", 200),
        ("/openapi.json", 404),
        ("/openapi.json?version=missing", 404),
        ("/openapi.json?version=unversioned", 404),
    ],
)
def test__openapi_customizer__request_without_document__should_not_call_customizer(url: str, status_code: int):
    def customize(schema: dict[str, Any], version: str | None) -> dict[str, Any]:
        raise AssertionError("Only served OpenAPI documents should invoke the customizer")

    with TestClient(Cadwyn(versions=_two_versions(), openapi_customizer=customize)) as client:
        assert client.get(url).status_code == status_code


def test__openapi_customizer__repeated_requests__should_isolate_versions_and_regenerate_nested_dicts():
    def customize(schema: dict[str, Any], version: str | None) -> dict[str, Any]:
        schema["info"].setdefault("x-visits", []).append(version)
        schema["components"]["schemas"]["HTTPValidationError"].setdefault("x-visits", []).append(version)
        schema["servers"][0]["url"] += "/customized"
        return schema

    app = Cadwyn(versions=_two_versions(), servers=[{"url": "/api"}], openapi_customizer=customize)
    router = VersionedAPIRouter()

    @router.get("/items")
    def get_items() -> list[str]:
        raise NotImplementedError

    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        for version in ("2022-02-02", "2021-01-01", "2022-02-02", "2021-01-01"):
            response = client.get("/openapi.json", params={"version": version})
            assert response.status_code == 200
            schema = response.json()
            assert schema["info"]["version"] == version
            assert schema["info"]["x-visits"] == [version]
            assert schema["components"]["schemas"]["HTTPValidationError"]["x-visits"] == [version]
            assert schema["servers"] == [{"url": "/api/customized"}]


@pytest.mark.parametrize("root_path", ["", "/proxy"])
@pytest.mark.parametrize("root_path_in_servers", [True, False])
@pytest.mark.parametrize("version", ["2022-02-02", "unversioned"])
def test__openapi_customizer__mounted_app__should_receive_final_servers_and_allow_replacing_them(
    root_path: str, root_path_in_servers: bool, version: str
):
    def customize(schema: dict[str, Any], selected_version: str | None) -> dict[str, Any]:
        return {"original_servers": schema["servers"], "servers": [{"url": "/custom"}]}

    app = Cadwyn(
        versions=_two_versions(),
        servers=[{"url": "/configured"}],
        root_path_in_servers=root_path_in_servers,
        openapi_customizer=customize,
    )

    @app.get("/health")
    def health() -> str:
        raise NotImplementedError

    root_app = FastAPI()
    root_app.mount("/mounted", app)
    with TestClient(root_app, root_path=root_path) as client:
        response = client.get(f"{root_path}/mounted/openapi.json", params={"version": version})
    assert response.status_code == 200
    expected_servers = [{"url": f"{root_path}/mounted"}, {"url": "/configured"}]
    assert response.json() == {
        "original_servers": expected_servers if root_path_in_servers else [{"url": "/configured"}],
        "servers": [{"url": "/custom"}],
    }


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


def test__get_openapi__api_version_header_named_with_underscore__should_document_and_require_that_header():
    app = Cadwyn(versions=VersionBundle(Version("2023-04-14")), api_version_parameter_name="api_version")
    router = VersionedAPIRouter()

    @router.get("/items")
    async def get_items():
        return []

    app.generate_and_include_versioned_routers(router)

    assert [
        (parameter["name"], parameter["required"])
        for parameter in _get_api_version_parameters_from_openapi(app, "/items")["2023-04-14"]
    ] == [("api_version", True)]
    with TestClient(app) as client:
        assert client.get("/items", headers={"api_version": "2023-04-14"}).json() == []


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


def test__api_version_context__waterfalling__should_keep_background_context_and_isolate_outer_middleware():
    versions = _two_versions()
    context = ContextVar("application_context", default="original")
    observed: list[tuple[str, str | None, str]] = []
    app = Cadwyn(versions=versions)
    router = VersionedAPIRouter()

    class ObserveContext:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def observe_headers(message: Message) -> None:
                if message["type"] == "http.response.start":
                    observed.append(("headers", versions.api_version_var.get(), context.get()))
                await send(message)

            await self.app(scope, receive, observe_headers)
            if scope["type"] == "http":
                observed.append(("after", versions.api_version_var.get(), context.get()))

    app.add_middleware(ObserveContext)

    @router.get("/items")
    async def get_items(background_tasks: BackgroundTasks):
        context.set("endpoint")
        observed.append(("endpoint", versions.api_version_var.get(), context.get()))

        async def background():
            context.set("background")
            observed.append(("background", versions.api_version_var.get(), context.get()))

        background_tasks.add_task(background)
        return "ok"

    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        response = client.get("/items", headers={"x-api-version": "2021-06-01"})

    assert response.json() == "ok"
    assert response.headers["x-api-version"] == "2021-01-01"
    assert observed == [
        ("endpoint", "2021-01-01", "endpoint"),
        ("headers", "2021-06-01", "original"),
        ("background", "2021-01-01", "background"),
        ("after", "2021-06-01", "original"),
    ]


@pytest.mark.parametrize("error", [HTTPException(400, "handled"), RuntimeError("unhandled")])
def test__api_version_context__waterfalling_with_exception__should_reach_exception_handler(error: Exception):
    versions = _two_versions()
    app = Cadwyn(versions=versions)
    router = VersionedAPIRouter()

    @app.exception_handler(type(error))
    async def handle_error(request: Request, exc: Exception):
        return JSONResponse({"version": versions.api_version_var.get()})

    @router.get("/items")
    async def get_items():
        raise error

    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        response = client.get("/items", headers={"x-api-version": "2021-06-01"})

    assert response.json() == {"version": "2021-01-01"}
    assert response.headers["x-api-version"] == "2021-01-01"


def test__api_version_context__waterfalling_with_unhandled_exception__should_reach_server_error_handler():
    versions = _two_versions()
    context = ContextVar("application_context", default="original")
    app = Cadwyn(versions=versions)
    router = VersionedAPIRouter()

    @app.exception_handler(Exception)
    async def handle_error(request: Request, exc: Exception):
        return JSONResponse({"version": versions.api_version_var.get(), "context": context.get()}, status_code=500)

    @router.get("/items")
    async def get_items():
        context.set("endpoint")
        raise RuntimeError("unhandled")

    app.generate_and_include_versioned_routers(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/items", headers={"x-api-version": "2021-06-01"})

    assert response.status_code == 500
    assert response.json() == {"version": "2021-06-01", "context": "original"}


def test__file_cleanup__mounted_fastapi_app__should_keep_host_cleanup_and_response():
    app = Cadwyn(versions=_two_versions())
    mounted = FastAPI()
    uploads = []

    @mounted.post("/test")
    async def endpoint(upload: UploadFile):
        uploads.append(upload)
        return {"size": upload.size}

    app.mount("/mounted", mounted)
    body = b"x" * (2 * 1024 * 1024)
    with TestClient(app) as client:
        response = client.post("/mounted/test", files={"upload": body})

    assert response.status_code == 200
    assert response.json() == {"size": len(body)}
    assert len(uploads) == 1
    assert uploads[0].file.closed


def test__file_cleanup__additional_fastapi_callback__should_keep_arguments_identity_and_context():
    app = Cadwyn(versions=_two_versions())
    context = ContextVar("cleanup_context", default="original")
    observed: list[tuple[str, str]] = []

    async def cleanup(value: str):
        observed.append((value, context.get()))

    @app.get("/test")
    async def endpoint(request: Request):
        context.set("endpoint")
        # FastAPI's file stack is also an AsyncExitStack for application-registered cleanup.
        registered = request.scope["fastapi_middleware_astack"].push_async_callback(cleanup, "closed")
        assert registered is cleanup
        return "unchanged"

    with TestClient(app) as client:
        response = client.get("/test")

    assert response.status_code == 200
    assert response.json() == "unchanged"
    assert observed == [("closed", "endpoint")]


@pytest.mark.parametrize("route_kind", ["unversioned", "versioned"])
@pytest.mark.parametrize(
    ("behavior", "expected_seen", "expected_outcome"),
    [
        ("success", ["success"], "200"),
        ("endpoint_error", ["ValueError"], "ValueError: endpoint failed"),
        ("exit_suppresses", ["ValueError"], "RuntimeError: No response returned."),
        ("exit_raises", ["ValueError"], "KeyError: 'exit failed'"),
        ("exit_raises_on_success", ["success"], "KeyError: 'exit failed'"),
    ],
)
def test__file_cleanup__context_manager_on_fastapi_file_stack__should_exit_like_in_fastapi(
    route_kind: Literal["unversioned", "versioned"],
    behavior: str,
    expected_seen: list[str],
    expected_outcome: str,
):
    seen: list[str] = []

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        try:
            yield
        except ValueError as exc:
            seen.append(type(exc).__name__)
            if behavior == "exit_raises":
                raise KeyError("exit failed") from None
            if behavior != "exit_suppresses":
                raise
        else:
            seen.append("success")
            if behavior == "exit_raises_on_success":
                raise KeyError("exit failed")

    async def endpoint(request: Request):
        # FastAPI's file stack is an ordinary AsyncExitStack, so apps can enter their own context managers on it
        await request.scope["fastapi_middleware_astack"].enter_async_context(transaction())
        if behavior in ("endpoint_error", "exit_suppresses", "exit_raises"):
            raise ValueError("endpoint failed")
        return "ok"

    async def passthrough(request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
        return await call_next(request)

    baseline = FastAPI()
    baseline.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    baseline.get("/test")(endpoint)
    app = Cadwyn(versions=_two_versions())
    if route_kind == "unversioned":
        app.get("/test")(endpoint)
    else:
        router = VersionedAPIRouter()
        router.get("/test")(endpoint)
        app.generate_and_include_versioned_routers(router)

    observed: list[tuple[list[str], str]] = []
    for target, headers in (
        (baseline, {}),
        (app, {"x-api-version": "2021-01-01"} if route_kind == "versioned" else {}),
    ):
        seen.clear()
        try:
            with TestClient(target) as client:
                outcome = str(client.get("/test", headers=headers).status_code)
        except (ValueError, RuntimeError, KeyError) as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        observed.append((seen.copy(), outcome))

    assert observed == [(expected_seen, expected_outcome)] * 2


@pytest.mark.parametrize("group_depth", [0, 1, 2])
def test__versioning_middleware__endpoint_exception__should_keep_base_http_middleware_exception_type(group_depth: int):
    async def passthrough(request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
        return await call_next(request)

    baseline = FastAPI()
    baseline.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    app = Cadwyn(versions=_two_versions())
    error: Exception = ValueError("unchanged")
    for _ in range(group_depth):
        error = ExceptionGroup("group", [error])

    async def endpoint():
        raise error

    baseline.get("/items")(endpoint)
    app.get("/items")(endpoint)
    observed: list[Exception] = []
    for target in (baseline, app):
        with TestClient(target) as client, pytest.raises((ValueError, ExceptionGroup)) as caught:
            client.get("/items")
        observed.append(caught.value)

    assert observed[0] is observed[1]


def test__get_openapi__nonexisting_version__should_return_404():
    resp = client_without_headers.get("/openapi.json?version=2023-01-01")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "OpenApi file with version `2023-01-01` not found"}


def test__get_openapi__with_mounted_app__should_include_root_path_in_servers():
    root_app = FastAPI()
    root_app.mount("/my_api", Cadwyn(versions=VersionBundle(Version("2022-11-16"))))
    client = TestClient(root_app)

    resp = client.get("/my_api/openapi.json?version=2022-11-16")
    servers = resp.json()["servers"]
    assert "/my_api" in [server["url"] for server in servers]


def test__cadwyn__with_openapi_prefix__should_raise_type_error():
    with pytest.raises(TypeError, match=re.escape("unexpected keyword argument 'openapi_prefix'")):
        Cadwyn(versions=VersionBundle(Version("2022-11-16")), openapi_prefix="/my_api")


def test__get_docs__without_unversioned_routes__should_return_all_versioned_doc_urls():
    app = Cadwyn(versions=_two_versions())
    app.generate_and_include_versioned_routers(versioned_router)

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
    app = Cadwyn(versions=VersionBundle(Version("2022-11-16")))
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

    app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2022-11-16")))
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    with TestClient(app) as client:
        resp = client.get("/static/hello.txt")

    assert resp.status_code == 200
    assert resp.text == "Hello World"


def test__get_docs__with_unversioned_routes__should_return_all_versioned_doc_urls():
    app = Cadwyn(versions=_two_versions())
    app.generate_and_include_versioned_routers(versioned_router)

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


def _add_background_task(
    background_tasks: BackgroundTasks, solver: Annotated[str, Depends(current_dependency_solver)]
) -> None:
    background_tasks.add_task(_background_task_calls.append, f"dependency of {solver}")


def _dependency_of_dependency(_: Annotated[None, Depends(_add_background_task)]) -> None:
    pass


def _yield_dependency(
    background_tasks: BackgroundTasks, solver: Annotated[str, Depends(current_dependency_solver)]
) -> Iterator[None]:
    background_tasks.add_task(_background_task_calls.append, f"dependency of {solver}")
    yield


def _add_background_task_once(
    background_tasks: BackgroundTasks, solver: Annotated[str, Depends(current_dependency_solver)]
) -> None:
    if solver == "fastapi":
        background_tasks.add_task(_background_task_calls.append, f"dependency of {solver}")


_background_task_calls: list[str] = []
_DEPENDENCIES_THAT_ADD_BACKGROUND_TASKS = [_add_background_task, _dependency_of_dependency, _yield_dependency]
_DROPPED_BACKGROUND_TASK_WARNING = (
    r"Cadwyn has dropped a background task of \"\['POST'\] {path}\".*Cadwyn 10\.0 will run such tasks.*"
    r"run_background_tasks_of_latest_version_dependencies=True.*current_dependency_solver"
)


@pytest.fixture
def background_task_calls() -> Iterator[list[str]]:
    _background_task_calls.clear()
    yield _background_task_calls
    _background_task_calls.clear()


def _client_of(
    router: VersionedAPIRouter,
    *version_changes: type[VersionChange],
    run_background_tasks_of_latest_version_dependencies: bool = False,
) -> TestClient:
    app = Cadwyn(
        versions=VersionBundle(Version("2022-02-02", *version_changes), Version(DEFAULT_API_VERSION)),
        run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies,
    )
    app.generate_and_include_versioned_routers(router)
    return TestClient(app, headers=BASIC_HEADERS)


# Tests treat warnings as errors, so the tests below also check that Cadwyn doesn't warn unless they expect it


@pytest.mark.parametrize("dependency", _DEPENDENCIES_THAT_ADD_BACKGROUND_TASKS)
def test__background_tasks__added_by_dependencies_of_endpoint_without_background_tasks__should_drop_the_tasks_of_the_latest_version_and_warn_once_per_route(  # noqa: E501
    background_task_calls: list[str], dependency: Callable[..., Any]
):
    router = VersionedAPIRouter()

    @router.post("/first", dependencies=[Depends(dependency)])
    async def first():
        background_task_calls.append("endpoint")

    @router.post("/second", dependencies=[Depends(dependency)])
    async def second():
        background_task_calls.append("endpoint")

    with _client_of(router) as client, warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        assert client.post("/first").status_code == 200
        assert client.post("/first").status_code == 200
        assert client.post("/first", headers={"X-API-VERSION": "2022-02-02"}).status_code == 200
        assert client.post("/second").status_code == 200

    assert [warning.category for warning in caught_warnings] == [FutureWarning, FutureWarning]
    assert re.match(_DROPPED_BACKGROUND_TASK_WARNING.format(path="/first"), str(caught_warnings[0].message))
    assert re.match(_DROPPED_BACKGROUND_TASK_WARNING.format(path="/second"), str(caught_warnings[1].message))
    assert background_task_calls == ["endpoint", "dependency of fastapi"] * 4


def test__background_tasks__added_by_dependency_that_only_the_latest_version_has__should_be_dropped_with_a_warning(
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        pass

    class RemoveDependency(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = (endpoint("/test", ["POST"]).had(dependencies=[]),)

    with (
        _client_of(router, RemoveDependency) as client,
        pytest.warns(FutureWarning, match=_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test")),
    ):
        assert client.post("/test").status_code == 200
    assert background_task_calls == []


@pytest.mark.parametrize("dependency", _DEPENDENCIES_THAT_ADD_BACKGROUND_TASKS)
def test__background_tasks__added_by_dependencies_with_run_background_tasks_of_latest_version_dependencies__should_run_once_per_solve(  # noqa: E501
    background_task_calls: list[str], dependency: Callable[..., Any]
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(dependency)])
    async def handler():
        background_task_calls.append("endpoint")

    with _client_of(router, run_background_tasks_of_latest_version_dependencies=True) as client:
        assert client.post("/test").status_code == 200
    assert background_task_calls == ["endpoint", "dependency of fastapi", "dependency of cadwyn"]


@pytest.mark.parametrize("run_background_tasks_of_latest_version_dependencies", [False, True])
@pytest.mark.parametrize("dependency", _DEPENDENCIES_THAT_ADD_BACKGROUND_TASKS)
def test__background_tasks__added_by_dependencies_of_endpoint_with_background_tasks__should_run_once_per_solve(
    background_task_calls: list[str],
    dependency: Callable[..., Any],
    run_background_tasks_of_latest_version_dependencies: bool,
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(dependency)])
    async def handler(background_tasks: BackgroundTasks):
        background_tasks.add_task(background_task_calls.append, "endpoint task")
        background_task_calls.append("endpoint")

    with _client_of(
        router, run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies
    ) as client:
        assert client.post("/test").status_code == 200
    assert background_task_calls == ["endpoint", "dependency of fastapi", "dependency of cadwyn", "endpoint task"]


@pytest.mark.parametrize("run_background_tasks_of_latest_version_dependencies", [False, True])
def test__background_tasks__added_by_dependency_only_for_the_request_version__should_run_once_without_a_warning(
    background_task_calls: list[str], run_background_tasks_of_latest_version_dependencies: bool
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task_once)])
    async def handler():
        pass

    with _client_of(
        router, run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies
    ) as client:
        assert client.post("/test").status_code == 200
    assert background_task_calls == ["dependency of fastapi"]


def test__background_tasks__added_by_dependency_that_only_the_latest_version_has_with_run_background_tasks_of_latest_version_dependencies__should_run(  # noqa: E501
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        pass

    class RemoveDependency(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = (endpoint("/test", ["POST"]).had(dependencies=[]),)

    with _client_of(router, RemoveDependency, run_background_tasks_of_latest_version_dependencies=True) as client:
        assert client.post("/test").status_code == 200
    assert background_task_calls == ["dependency of cadwyn"]


def test__background_tasks__with_returned_response_without_background_and_run_background_tasks_of_latest_version_dependencies__should_run_tasks_of_both_solves(  # noqa: E501
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        return JSONResponse("ok")

    with _client_of(router, run_background_tasks_of_latest_version_dependencies=True) as client:
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["dependency of fastapi", "dependency of cadwyn"]


def test__background_tasks__with_returned_response_without_background__should_drop_the_tasks_of_the_latest_version_with_a_warning(  # noqa: E501
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        return JSONResponse("ok")

    with (
        _client_of(router) as client,
        pytest.warns(FutureWarning, match=_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test")),
    ):
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["dependency of fastapi"]


async def _respond_with_own_background():
    return JSONResponse("ok", background=BackgroundTask(_background_task_calls.append, "response"))


async def _respond_with_own_background_and_background_tasks(background_tasks: BackgroundTasks):
    background_tasks.add_task(_background_task_calls.append, "endpoint task")
    return JSONResponse("ok", background=BackgroundTask(_background_task_calls.append, "response"))


@pytest.mark.parametrize("run_background_tasks_of_latest_version_dependencies", [False, True])
@pytest.mark.parametrize("handler", [_respond_with_own_background, _respond_with_own_background_and_background_tasks])
def test__background_tasks__with_returned_response_with_its_own_background__should_run_only_it_as_in_fastapi(
    background_task_calls: list[str],
    run_background_tasks_of_latest_version_dependencies: bool,
    handler: Callable[..., Any],
):
    router = VersionedAPIRouter()
    router.post("/test", dependencies=[Depends(_add_background_task)])(handler)

    with _client_of(
        router, run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies
    ) as client:
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["response"]


def test__background_tasks__with_streaming_response_and_run_background_tasks_of_latest_version_dependencies__should_run_after_the_stream(  # noqa: E501
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        async def stream() -> AsyncIterator[bytes]:
            background_task_calls.append("stream")
            yield b"ok"

        return StreamingResponse(stream())

    with _client_of(router, run_background_tasks_of_latest_version_dependencies=True) as client:
        assert client.post("/test").content == b"ok"
    assert background_task_calls == ["stream", "dependency of fastapi", "dependency of cadwyn"]


def test__background_tasks__with_streaming_response__should_drop_the_tasks_of_the_latest_version_with_a_warning(
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        async def stream() -> AsyncIterator[bytes]:
            background_task_calls.append("stream")
            yield b"ok"

        return StreamingResponse(stream())

    with (
        _client_of(router) as client,
        pytest.warns(FutureWarning, match=_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test")),
    ):
        assert client.post("/test").content == b"ok"
    assert background_task_calls == ["stream", "dependency of fastapi"]


async def _raise_http_exception():
    raise HTTPException(status_code=400)


async def _raise_http_exception_with_background_tasks(background_tasks: BackgroundTasks):
    background_tasks.add_task(_background_task_calls.append, "endpoint task")
    raise HTTPException(status_code=400)


@pytest.mark.parametrize("run_background_tasks_of_latest_version_dependencies", [False, True])
@pytest.mark.parametrize("handler", [_raise_http_exception, _raise_http_exception_with_background_tasks])
def test__background_tasks__when_endpoint_raises_http_exception__should_not_run_without_a_warning(
    background_task_calls: list[str],
    run_background_tasks_of_latest_version_dependencies: bool,
    handler: Callable[..., Any],
):
    router = VersionedAPIRouter()
    router.post("/test", dependencies=[Depends(_add_background_task)])(handler)

    with _client_of(
        router, run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies
    ) as client:
        assert client.post("/test").status_code == 400
    assert background_task_calls == []


def test__background_tasks__dropped_while_future_warnings_are_errors__should_log_the_warning_once_and_keep_the_response(
    background_task_calls: list[str], caplog: pytest.LogCaptureFixture
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        return "ok"

    # Tests turn every warning into an error
    with _client_of(router) as client:
        assert client.post("/test").json() == "ok"
        assert client.post("/test").json() == "ok"

    assert background_task_calls == ["dependency of fastapi"] * 2
    dropped_task_logs = [record for record in caplog.records if record.name.startswith("cadwyn")]
    assert [record.levelno for record in dropped_task_logs] == [logging.WARNING]
    assert re.match(_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test"), dropped_task_logs[0].getMessage())


async def _endpoint_with_background_tasks_named_like_cadwyn_param(cadwyn_background_tasks_param: BackgroundTasks):
    cadwyn_background_tasks_param.add_task(_background_task_calls.append, "endpoint task")
    return "ok"


@pytest.mark.parametrize("run_background_tasks_of_latest_version_dependencies", [False, True])
def test__background_tasks__with_endpoint_param_named_cadwyn_background_tasks_param__should_get_the_background_tasks(
    background_task_calls: list[str], run_background_tasks_of_latest_version_dependencies: bool
):
    router = VersionedAPIRouter()
    router.post("/test", dependencies=[Depends(_add_background_task)])(
        _endpoint_with_background_tasks_named_like_cadwyn_param
    )

    with _client_of(
        router, run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies
    ) as client:
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["dependency of fastapi", "dependency of cadwyn", "endpoint task"]


@pytest.mark.parametrize("run_background_tasks_of_latest_version_dependencies", [False, True])
def test__background_tasks__with_query_param_named_cadwyn_background_tasks_param__should_get_the_query_value(
    run_background_tasks_of_latest_version_dependencies: bool,
):
    router = VersionedAPIRouter()

    @router.post("/test")
    async def handler(cadwyn_background_tasks_param: str = "default"):
        return cadwyn_background_tasks_param

    with _client_of(
        router, run_background_tasks_of_latest_version_dependencies=run_background_tasks_of_latest_version_dependencies
    ) as client:
        assert client.post("/test", params={"cadwyn_background_tasks_param": "value"}).json() == "value"
        assert client.post("/test").json() == "default"


def _add_background_task_after_yield(
    background_tasks: BackgroundTasks, solver: Annotated[str, Depends(current_dependency_solver)]
) -> Iterator[None]:
    yield
    background_tasks.add_task(_background_task_calls.append, f"dependency of {solver}")


def test__background_tasks__added_only_after_yield_of_function_scoped_dependency__should_be_dropped_without_a_warning(
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task_after_yield, scope="function")])
    async def handler():
        return "ok"

    # A documented limitation: Cadwyn decides whether to warn when the endpoint returns, before the teardown
    with _client_of(router) as client:
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["dependency of fastapi"]


def test__background_tasks__added_after_yield_of_function_scoped_dependency_with_run_background_tasks_of_latest_version_dependencies__should_run(  # noqa: E501
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task_after_yield, scope="function")])
    async def handler():
        return "ok"

    with _client_of(router, run_background_tasks_of_latest_version_dependencies=True) as client:
        assert client.post("/test").json() == "ok"
    assert sorted(background_task_calls) == ["dependency of cadwyn", "dependency of fastapi"]


def _add_background_task_and_raise_after_yield(
    background_tasks: BackgroundTasks,
    solver: Annotated[str, Depends(current_dependency_solver)],
    fail_after_yield: bool = False,
) -> Iterator[None]:
    background_tasks.add_task(_background_task_calls.append, f"dependency of {solver}")
    yield
    if fail_after_yield:
        raise HTTPException(status_code=400)


@pytest.mark.parametrize(
    ("params", "expected_status_code"),
    [
        pytest.param({"valid_response": False}, 500, id="response_validation_error"),
        pytest.param({"fail_after_yield": True}, 400, id="http_exception_after_yield"),
    ],
)
def test__background_tasks__dropped_in_request_that_fails_after_the_solve__should_not_warn_until_a_request_succeeds(
    background_task_calls: list[str], params: dict[str, Any], expected_status_code: int
):
    router = VersionedAPIRouter()

    @router.post(
        "/test",
        dependencies=[Depends(_add_background_task_and_raise_after_yield, scope="function")],
        response_model=int,
    )
    async def handler(valid_response: bool = True):
        return 1 if valid_response else "not an integer"

    app = _client_of(router).app
    with (
        TestClient(app, headers=BASIC_HEADERS, raise_server_exceptions=False) as client,
        warnings.catch_warnings(record=True) as caught_warnings,
    ):
        warnings.simplefilter("always")
        assert client.post("/test", params=params).status_code == expected_status_code
        assert caught_warnings == []
        assert background_task_calls == []

        assert client.post("/test").json() == 1

    assert [warning.category for warning in caught_warnings] == [FutureWarning]
    assert re.match(_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test"), str(caught_warnings[0].message))
    assert background_task_calls == ["dependency of fastapi"]


class _FailingLogHandler(logging.Handler):
    """A log handler whose transport fails, e.g. a `FileHandler` whose directory is gone."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[threading.Thread] = []

    @override
    def handle(self, record: logging.LogRecord) -> bool:
        self.threads.append(threading.current_thread())
        raise OSError("The log transport is unavailable")


@pytest.fixture
def failing_log_handler() -> Iterator[_FailingLogHandler]:
    handler = _FailingLogHandler()
    logger = logging.getLogger("cadwyn")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def _drop_background_task_and_add_one_after_yield(
    background_tasks: BackgroundTasks, solver: Annotated[str, Depends(current_dependency_solver)]
) -> Iterator[None]:
    if solver == "cadwyn":
        background_tasks.add_task(_background_task_calls.append, "dropped")
    yield
    if solver == "fastapi":
        background_tasks.add_task(_background_task_calls.append, "user task")


def test__background_tasks__dropped_while_warnings_are_errors_and_logging_fails__should_still_run_the_user_tasks(
    background_task_calls: list[str], failing_log_handler: _FailingLogHandler
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_drop_background_task_and_add_one_after_yield, scope="function")])
    async def handler():
        return "ok"

    # Tests turn every warning into an error, so Cadwyn falls back to logging, which fails too
    with _client_of(router) as client:
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["user task"]
    assert len(failing_log_handler.threads) == 1


def test__background_tasks__dropped_while_warnings_are_errors__should_log_the_warning_off_the_event_loop(
    background_task_calls: list[str], failing_log_handler: _FailingLogHandler
):
    router = VersionedAPIRouter()
    event_loop_threads: list[threading.Thread] = []

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        event_loop_threads.append(threading.current_thread())
        return "ok"

    with _client_of(router) as client:
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["dependency of fastapi"]
    assert len(event_loop_threads) == len(failing_log_handler.threads) == 1
    assert failing_log_handler.threads[0] is not event_loop_threads[0]


class _RouteThatRecordsBackgroundTasks(APIRoute):
    """Records how many background tasks the response has, or "None" if it has no background."""

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def handle_and_record_background_tasks(request: Request) -> Response:
            response = await handle(request)
            background = response.background
            response.headers["x-background-tasks"] = (
                "None" if background is None else str(len(cast("BackgroundTasks", background).tasks))
            )
            return response

        return handle_and_record_background_tasks


def _router_with_route_that_returns_its_params(dependencies: list[Any]) -> VersionedAPIRouter:
    router = VersionedAPIRouter(route_class=_RouteThatRecordsBackgroundTasks)

    @router.post("/test", dependencies=dependencies)
    async def handler(request: Request):
        route = request.scope["route"]
        return {
            "background_tasks_param_name": route.dependant.background_tasks_param_name,
            "params": list(inspect.signature(route.endpoint).parameters),
        }

    return router


_PARAMS_OF_ROUTE_WITHOUT_BACKGROUND_TASKS = {
    "background_tasks_param_name": None,
    "params": ["request", "cadwyn_response_param"],
}


def test__background_tasks__of_route_without_background_tasks__should_leave_the_route_and_the_response_as_in_fastapi():
    with _client_of(_router_with_route_that_returns_its_params([])) as client:
        response = client.post("/test")

    assert response.json() == _PARAMS_OF_ROUTE_WITHOUT_BACKGROUND_TASKS
    assert response.headers["x-background-tasks"] == "None"


def test__background_tasks__of_route_that_drops_no_tasks__should_leave_the_route_and_the_response_as_in_fastapi(
    background_task_calls: list[str],
):
    router = _router_with_route_that_returns_its_params([Depends(_add_background_task_once)])

    with _client_of(router) as client:
        response = client.post("/test")

    assert response.json() == _PARAMS_OF_ROUTE_WITHOUT_BACKGROUND_TASKS
    assert response.headers["x-background-tasks"] == "1"
    assert background_task_calls == ["dependency of fastapi"]


def test__background_tasks__of_route_that_drops_tasks__should_leave_the_route_and_the_response_as_in_fastapi(
    background_task_calls: list[str],
):
    router = _router_with_route_that_returns_its_params([Depends(_add_background_task)])

    with (
        _client_of(router) as client,
        pytest.warns(FutureWarning, match=_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test")),
    ):
        response = client.post("/test")

    assert response.json() == _PARAMS_OF_ROUTE_WITHOUT_BACKGROUND_TASKS
    assert response.headers["x-background-tasks"] == "1"
    assert background_task_calls == ["dependency of fastapi"]


def test__background_tasks__dropped_in_concurrent_requests__should_warn_once(background_task_calls: list[str]):
    router = VersionedAPIRouter()
    both_requests_have_arrived = asyncio.Barrier(2)

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler():
        # Both requests return before either of them warns
        await both_requests_have_arrived.wait()
        return "ok"

    app = _client_of(router).app

    async def send_two_requests() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test", headers=BASIC_HEADERS
        ) as client:
            return await asyncio.gather(client.post("/test"), client.post("/test"))

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        responses = asyncio.run(send_two_requests())

    assert [response.json() for response in responses] == ["ok", "ok"]
    assert [warning.category for warning in caught_warnings] == [FutureWarning]
    assert background_task_calls == ["dependency of fastapi"] * 2


def test__background_tasks__dropped_without_fastapi_request_exit_stack__should_warn_right_away(
    background_task_calls: list[str],
):
    router = VersionedAPIRouter()

    @router.post("/test", dependencies=[Depends(_add_background_task)])
    async def handler(request: Request):
        # Every supported FastAPI version sets it, so we remove it to see what Cadwyn does without it
        request.scope.pop("fastapi_inner_astack")
        return "ok"

    with (
        _client_of(router) as client,
        pytest.warns(
            FutureWarning,
            match=_DROPPED_BACKGROUND_TASK_WARNING.format(path="/test") + r".*couldn't defer this warning",
        ),
    ):
        assert client.post("/test").json() == "ok"
    assert background_task_calls == ["dependency of fastapi"]


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


def test__cadwyn__with_removed_api_version_header_name__should_raise_type_error():
    with pytest.raises(TypeError, match=re.escape("Use api_version_parameter_name instead")):
        Cadwyn(api_version_header_name="x-api-version", versions=VersionBundle(Version("2022-11-16")))


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
