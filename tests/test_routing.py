import re
from typing import TYPE_CHECKING

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Match, NoMatchFound
from starlette.testclient import TestClient

from cadwyn import Cadwyn, VersionChange, endpoint
from cadwyn.exceptions import CadwynError
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.versions import HeadVersion, Version, VersionBundle
from tests._resources.app_for_testing_routing import mixed_hosts_app

if TYPE_CHECKING:
    from collections.abc import Callable


def test__populate_routes__with_versioned_and_unversioned_routes__should_combine_both_route_sets():
    versioned_routes = [
        route for router in mixed_hosts_app.router.versioned_routers.values() for route in router.routes
    ]
    assert sorted(mixed_hosts_app.router.routes, key=id) == sorted(
        mixed_hosts_app.router.unversioned_routes + versioned_routes,
        key=id,
    )


def test__header_routing__with_defined_and_compatible_versions__should_select_requested_or_closest_earlier_version():
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


def test__cadwyn_router__used_as_asgi_app__should_serve_plain_http_route():
    app = Cadwyn(versions=VersionBundle(Version("2024-01-01")))

    async def plain_route(request: Request):
        return PlainTextResponse("unchanged")

    app.add_route("/plain", plain_route, methods=["GET"])
    with TestClient(app.router) as client:
        response = client.get("/plain")
    assert response.status_code == 200
    assert response.text == "unchanged"


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


@pytest.mark.parametrize("version", ["bad", "2024-99-01"])
def test__header_routing__invalid_version_in_header_named_with_underscore__should_return_422(version: str):
    app = Cadwyn(
        versions=VersionBundle(Version("2024-02-01"), Version("2024-01-01")), api_version_parameter_name="api_version"
    )
    router = VersionedAPIRouter()

    @router.get("/ping")
    async def ping():
        return "pong"

    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        assert client.get("/ping", headers={"api_version": "2024-01-15"}).json() == "pong"
        response = client.get("/ping", headers={"api_version": version})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["header", "api_version"]


def test__header_routing__path_match_with_wrong_method__should_return_405():
    client = TestClient(mixed_hosts_app, headers={"X-API-VERSION": "2022-02-11"})

    response = client.post("/v1/users/tom/83")
    assert response.status_code == 405


def test__url_path_for__missing_path_parameter__should_raise_no_match_found():
    with pytest.raises(
        NoMatchFound,
        match=re.escape('No route exists for name "api:users" and params "username".'),
    ):
        mixed_hosts_app.url_path_for("api:users", username="tom")


def test__url_path_for__parameters_for_child_route_passed_to_mount__should_raise_no_match_found():
    with pytest.raises(
        NoMatchFound,
        match=re.escape('No route exists for name "api" and params "path, username".'),
    ):
        mixed_hosts_app.url_path_for("api", path="hellow", username="tom")


def test__lifespan__with_async_startup_and_shutdown_handlers__should_run_each_handler():
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


def _path_versioned_app(*values: str, prefix: str) -> Cadwyn:
    versions = VersionBundle(*(Version(value) for value in values))
    app = Cadwyn(
        versions=versions,
        api_version_location="path",
        api_version_parameter_name="api_version",
        api_version_format="date" if values[0][0].isdigit() else "string",
    )
    router = VersionedAPIRouter(prefix=prefix)

    @router.get("/ping")
    async def ping():
        return versions.api_version_var.get(None)

    app.generate_and_include_versioned_routers(router)
    return app


@pytest.mark.parametrize(
    ("prefix", "path", "expected"),
    [
        ("/{tenant}/{api_version}", "/2024-02-01/2024-01-01/ping", "2024-01-01"),
        ("/{api_version}/{tenant}", "/2024-01-01/2024-02-01/ping", "2024-01-01"),
    ],
)
def test__path_routing__declared_version_elsewhere_in_path__should_read_version_from_its_route_position(
    prefix: str, path: str, expected: str
):
    with TestClient(_path_versioned_app("2024-02-01", "2024-01-01", prefix=prefix)) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["api_version"] == expected


@pytest.mark.parametrize(
    ("values", "prefix", "path"),
    [
        (("v2", "v1"), "/{tenant}/{api_version}", "/v2/bad/ping"),
        (("2024-02-01", "2024-01-01"), "/{tenant}/{api_version}", "/2024-02-01/2024-01-15/ping"),
        (("2024-02-01", "2024-01-01"), "/{api_version}/{tenant}", "/bad/2024-01-01/ping"),
    ],
)
def test__path_routing__undeclared_version_next_to_declared_one__should_return_404(
    values: tuple[str, str], prefix: str, path: str
):
    with TestClient(_path_versioned_app(*values, prefix=prefix)) as client:
        assert client.get(path).status_code == 404


def test__path_routing__trailing_slash__should_redirect_within_the_version():
    with TestClient(_path_versioned_app("2024-02-01", "2024-01-01", prefix="/{api_version}")) as client:
        response = client.get("/2024-01-01/ping/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://testserver/2024-01-01/ping"


def test__path_routing__wrong_method__should_return_405():
    with TestClient(_path_versioned_app("2024-02-01", "2024-01-01", prefix="/{api_version}")) as client:
        assert client.post("/2024-01-01/ping").status_code == 405


def test__path_routing__url_matching_version_positions_of_several_versions__should_raise_cadwyn_error():
    class MoveVersionAfterTenant(VersionChange):
        description = "Put the tenant before the API version"
        instructions_to_migrate_to_previous_version = (
            endpoint("/{api_version}/{tenant}/ping", ["GET"]).had(path="/{tenant}/{api_version}/ping"),
        )

    versions = VersionBundle(Version("2024-02-01", MoveVersionAfterTenant), Version("2024-01-01"))
    app = Cadwyn(versions=versions, api_version_location="path", api_version_parameter_name="api_version")
    router = VersionedAPIRouter()

    @router.get("/{api_version}/{tenant}/ping")
    async def ping(tenant: str):
        return tenant

    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        assert client.get("/2024-02-01/acme/ping").json() == "acme"
    with (
        TestClient(app) as client,
        pytest.raises(CadwynError, match=r"matches routes of several API versions \(2024-02-01, 2024-01-01\)"),
    ):
        client.get("/2024-02-01/2024-01-01/ping")


@pytest.mark.parametrize(
    ("method", "path", "status_code", "body"),
    [
        ("GET", "/2024-01-01/items", 200, '"versioned"'),
        ("GET", "/2024-01-01/missing", 404, '{"detail":"Not Found"}'),
        ("GET", "/2024-01-01/items/", 307, ""),
        ("POST", "/2024-01-01/missing", 404, '{"detail":"Not Found"}'),
    ],
)
def test__path_routing__no_versioned_route_matches__should_keep_the_url_version_instead_of_unversioned_fallback(
    method: str, path: str, status_code: int, body: str
):
    app = Cadwyn(
        versions=VersionBundle(Version("2024-02-01"), Version("2024-01-01")),
        api_version_location="path",
        api_version_parameter_name="api_version",
    )
    router = VersionedAPIRouter()

    @router.get("/{api_version}/items")
    async def items():
        return "versioned"

    app.generate_and_include_versioned_routers(router)

    @app.get("/{path:path}")
    async def fallback(path: str):
        return "unversioned fallback"

    with TestClient(app) as client:
        assert client.get("/about").json() == "unversioned fallback"
        response = client.request(method, path, follow_redirects=False)
    assert response.status_code == status_code
    assert response.text == body
    assert response.headers["api_version"] == "2024-01-01"
