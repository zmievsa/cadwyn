from typing import TYPE_CHECKING

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Match, NoMatchFound
from starlette.testclient import TestClient

from cadwyn import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.versions import HeadVersion, Version, VersionBundle
from tests._resources.app_for_testing_routing import mixed_hosts_app

if TYPE_CHECKING:
    from collections.abc import Callable


def test__populate_routes__should_combine_unversioned_and_versioned_routes():
    versioned_routes = [
        route for router in mixed_hosts_app.router.versioned_routers.values() for route in router.routes
    ]
    assert sorted(mixed_hosts_app.router.routes, key=id) == sorted(
        mixed_hosts_app.router.unversioned_routes + versioned_routes,
        key=id,
    )


def test__header_routing__should_select_requested_or_closest_earlier_version():
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2022-02-11"})

    response = client.get("/v1/users/tom/83")
    assert response.status_code == 200
    assert response.json() == {"users": [{"username": "tom", "page": 83}]}

    response = client.get("/v1/")
    # its fine, because "/v1/" is defined in the lower version
    assert response.status_code == 200

    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2022-01-10"})

    response = client.get("/v1/users")
    assert response.status_code == 200
    assert response.text == "All users"

    response = client.get("/v1/")
    assert response.status_code == 200

    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2022-03-12"})

    response = client.get("/v1/users")
    # its fine, because /users is defined in the lower version
    assert response.status_code == 200

    response = client.get("/v1/")
    assert response.status_code == 200

    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2025-01-01"})

    response = client.get("/v1/users")
    # its fine, because /users is defined in the lower version
    assert response.status_code == 200


@pytest.mark.parametrize("version", ["2022-04-19", "2022-05-01", "2025-11-12"])
def test__header_routing__version_after_route_introduction__should_use_closest_earlier_route(version: str):
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": version})

    response = client.get("/v1/doggies/tom")
    assert response.status_code == 200
    assert response.json() == {"doggies": [{"dogname": "tom"}]}


def test__header_routing__version_before_route_introduction__should_return_404():
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "1993-11-15"})

    response = client.get("/v1/doggies/tom")
    assert response.status_code == 404


def test__header_routing__websocket_scope__should_not_match():
    assert mixed_hosts_app.routes[-1].matches({"type": "websocket", "path": "/v1/"}) == (Match.NONE, {})


def test__header_routing__invalid_date_header__should_return_422():
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2025-40-01"})

    response = client.get("/v1/users")
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "date_from_datetime_parsing",
                "loc": ["header", "x-api-version"],
                "msg": "Input should be a valid date or datetime, month value is outside expected range of 1-12",
                "input": "2025-40-01",
                "ctx": {"error": "month value is outside expected range of 1-12"},
            }
        ]
    }


def test__header_routing__path_match_with_wrong_method__should_return_405():
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2022-02-11"})

    response = client.post("/v1/users/tom/83")
    assert response.status_code == 405


def test__url_path_for__missing_path_parameter__should_raise_no_match_found():
    with pytest.raises(
        NoMatchFound,
        match='No route exists for name "api:users" and params "username".',
    ):
        mixed_hosts_app.url_path_for("api:users", username="tom")


def test__url_path_for__parameters_for_child_route_passed_to_mount__should_raise_no_match_found():
    with pytest.raises(
        NoMatchFound,
        match='No route exists for name "api" and params "path, username".',
    ):
        mixed_hosts_app.url_path_for("api", path="hellow", username="tom")


def test__lifespan__should_run_async_startup_and_shutdown_handlers():
    startup_complete = False
    shutdown_complete = False

    async def hello_world(request: Request):
        return PlainTextResponse("hello, world")

    async def run_startup():
        nonlocal startup_complete
        startup_complete = True

    async def run_shutdown():
        nonlocal shutdown_complete
        shutdown_complete = True

    app = Cadwyn(
        versions=VersionBundle(Version("2022-11-16")),
        on_startup=[run_startup],
        on_shutdown=[run_shutdown],
    )
    app.add_route("/v1/", hello_world)

    assert not startup_complete
    assert not shutdown_complete
    with TestClient(app) as client:
        assert startup_complete
        assert not shutdown_complete
        client.get("/v1/")
    assert startup_complete
    assert shutdown_complete


def test__header_routing__exact_oldest_version__should_route_successfully():
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "1998-11-16"})

    response = client.get("/v1/doggies/tom")
    assert response.status_code == 200


async def get_default_version(req: Request):
    return "2023-04-14"


@pytest.mark.parametrize("default_version", ["2023-04-14", get_default_version])
def test__default_version__unversioned_route__should_take_priority_over_versioned_route(
    default_version: "str | Callable",
):
    app = Cadwyn(
        versions=VersionBundle(HeadVersion(), Version("2023-04-14"), Version("2022-11-16")),
        api_version_default_value=default_version,
    )

    router = VersionedAPIRouter()

    @app.get("/my_duplicated_route")
    def get_my_unversioned_number():
        return 11

    @router.get("/my_duplicated_route")
    def get_my_versioned_number():
        return 83

    @router.get("/my_single_route")
    def get_my_versioned_number_2():
        return 52

    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        resp = client.get("/docs")
        assert resp.status_code == 200, resp.json()

        resp = client.get("/docs?version=2023-04-14")
        assert resp.status_code == 200, resp.json()

        resp = client.get("/docs?version=2022-11-16")
        assert resp.status_code == 200, resp.json()

        resp = client.get("/my_duplicated_route")
        assert resp.status_code == 200, resp.json()
        assert resp.json() == 11

        resp = client.get("/my_duplicated_route", headers={"X-API-VERSION": "2023-04-14"})
        assert resp.status_code == 200, resp.json()
        assert resp.json() == 83

        resp = client.get("/my_single_route")
        assert resp.status_code == 200, resp.json()
        assert resp.json() == 52
