import re
from datetime import date
from typing import Annotated, cast

import pytest
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cadwyn import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.versions import HeadVersion, Version, VersionBundle
from tests._resources.utils import BASIC_HEADERS, DEFAULT_API_VERSION
from tests._resources.versioned_app.app import (
    client_without_headers,
    client_without_headers_and_with_custom_api_version_var,
    v2021_01_01_router,
    v2022_01_02_router,
)


def test__header_routing__invalid_version_format__error():
    main_app = Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))))
    main_app.add_header_versioned_routers(APIRouter(), header_value=DEFAULT_API_VERSION)
    with pytest.raises(ValueError, match=re.escape("header_value should be in ISO 8601 format")):
        main_app.add_header_versioned_routers(APIRouter(), header_value="2022-01_01")


def test__header_routing_fastapi_init__openapi_passing_nulls__should_not_add_openapi_routes():
    assert [cast(APIRoute, r).path for r in Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16)))).routes] == [
        "/docs/oauth2-redirect",
        "/changelog",
        "/openapi.json",
        "/docs",
        "/redoc",
    ]
    assert [
        cast(APIRoute, r).path
        for r in Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))), docs_url=None, redoc_url=None).routes
    ] == [
        "/changelog",
        "/openapi.json",
    ]
    assert (
        Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))), openapi_url=None, changelog_url=None).routes == []
    )


def test__header_routing_fastapi_init__passing_null_to_oauth2__should_not_add_oauth2_redirect_route():
    app = Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))), swagger_ui_oauth2_redirect_url=None)
    assert [cast(APIRoute, r).path for r in app.routes] == [
        "/changelog",
        "/openapi.json",
        "/docs",
        "/redoc",
    ]
    app.add_header_versioned_routers(v2021_01_01_router, header_value="2021-01-01")

    with TestClient(app) as client:
        assert client.get("/docs?version=2021-01-01").status_code == 200


def test__header_routing_fastapi_init__changing_openapi_url__docs_still_return_200():
    app = Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))), openapi_url="/openpapi")
    app.add_header_versioned_routers(v2021_01_01_router, header_value="2021-01-01")
    app.add_header_versioned_routers(v2022_01_02_router, header_value="2022-02-02")
    with TestClient(app) as client:
        assert client.get("/openpapi?version=2021-01-01").status_code == 200
        assert client.get("/openapi.json?version=2021-01-01").status_code == 404


def test__header_routing_fastapi__calling_openapi_incorrectly__docs_should_return_404():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version(date(2022, 11, 16))))
    app.add_header_versioned_routers(v2021_01_01_router, header_value="2021-01-01")
    app.add_header_versioned_routers(v2022_01_02_router, header_value="2022-02-02")
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
    app = Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))))

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

    app.add_header_versioned_routers(regular_router, header_value="2022-11-16")

    versioned_router = VersionedAPIRouter()

    @versioned_router.post("/my_old_friend")
    async def my_old_friend(dependency: Annotated[str, Depends(old_dependency)]):
        return dependency

    app.generate_and_include_versioned_routers(versioned_router)

    app.dependency_overrides = {old_dependency: new_dependency}
    with TestClient(app) as client:
        assert client.post("/hello").json() == "new"
        assert client.post("/darkness", headers={"x-api-version": "2022-11-16"}).json() == "new"
        assert client.post("/my_old_friend", headers={"x-api-version": "2022-11-16"}).json() == "new"


def test__header_routing_fastapi_add_header_versioned_routers__apirouter_is_empty__version_should_not_have_any_routes():
    app = Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))))
    app.add_header_versioned_routers(APIRouter(), header_value="2022-11-16")
    assert len(app.router.versioned_routers) == 1
    assert len(app.router.versioned_routers[date(2022, 11, 16)].routes) == 1
    route = cast(APIRoute, app.router.versioned_routers[date(2022, 11, 16)].routes[0])
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
    assert resp.headers["X-API-VERSION"] == "2024-02-02"


def test__header_based_versioning__invalid_version_header_format__should_raise_422():
    resp = client_without_headers.get("/v1", headers=BASIC_HEADERS | {"X-API-VERSION": "2022-02_02"})
    assert resp.status_code == 422
    assert resp.json()[0]["loc"] == ["header", "x-api-version"]


def test__get_webhooks_router():
    resp = client_without_headers.post("/v1/webhooks")
    assert resp.status_code == 200
    assert resp.json() == {"saved": True}


def test__get_openapi():
    resp = client_without_headers.get("/openapi.json", headers={"x-api-version": "2021-01-01"})
    assert resp.status_code == 200

    resp = client_without_headers.get("/openapi.json?version=2021-01-01")
    assert resp.status_code == 200


def test__get_openapi__nonexisting_version__error():
    resp = client_without_headers.get("/openapi.json?version=2023-01-01")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "OpenApi file of with version `2023-01-01` not found"}


def test__get_openapi__with_mounted_app__should_include_root_path_in_servers():
    root_app = FastAPI()
    root_app.mount("/my_api", Cadwyn(changelog_url=None, versions=VersionBundle(Version(date(2022, 11, 16)))))
    client = TestClient(root_app)

    resp = client.get("/my_api/openapi.json?version=2022-11-16")
    servers = resp.json()["servers"]
    assert "/my_api" in [server["url"] for server in servers]


def test__get_docs__without_unversioned_routes__should_return_all_versioned_doc_urls():
    app = Cadwyn(changelog_url=None, versions=VersionBundle(Version(date(2022, 11, 16))))
    app.add_header_versioned_routers(v2021_01_01_router, header_value="2021-01-01")
    app.add_header_versioned_routers(v2022_01_02_router, header_value="2022-02-02")

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


def test__get_docs__with_mounted_app__should_return_all_versioned_doc_urls():
    root_app = FastAPI()
    root_app.mount("/my_api", Cadwyn(changelog_url=None, versions=VersionBundle(Version(date(2022, 11, 16)))))
    client = TestClient(root_app)

    resp = client.get("/my_api/docs")
    assert "http://testserver/my_api/docs?version=2022-11-16" in resp.content.decode()


def test__get_docs__with_unversioned_routes__should_return_all_versioned_doc_urls():
    app = Cadwyn(versions=VersionBundle(Version(date(2022, 11, 16))))
    app.add_header_versioned_routers(v2021_01_01_router, header_value="2021-01-01")
    app.add_header_versioned_routers(v2022_01_02_router, header_value="2022-02-02")

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


def test__get_webhooks_with_redirect():
    resp = client_without_headers.post("/v1/webhooks/")
    assert resp.status_code == 200
    assert resp.json() == {"saved": True}


def test__get_webhooks_as_partial_because_of_method():
    resp = client_without_headers.patch("/v1/webhooks")
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
