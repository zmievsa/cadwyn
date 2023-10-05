from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fastapi_header_versioning.fastapi import HeaderRoutingFastAPI
from fastapi_header_versioning.routing import HeaderVersionedAPIRouter

from universi import get_universi_dependency, regenerate_dir_to_all_versions
from universi.header_routing import get_versioned_router

from ..utils import clean_versions
from .schemas import latest
from .users import api_version_var, router, versions


def get_app(versioned_router: HeaderVersionedAPIRouter) -> HeaderRoutingFastAPI:
    app = HeaderRoutingFastAPI(
        version_header="x-api-version",
        dependencies=[get_universi_dependency(version_header_name="X-API-VERSION", api_version_var=api_version_var)],
    )
    app.include_router(versioned_router)
    return app


@pytest.fixture(scope="module", autouse=True)
def versioned_router() -> Generator[HeaderVersionedAPIRouter, None, None]:
    regenerate_dir_to_all_versions(latest, versions)
    try:
        yield get_versioned_router(router, versions=versions, latest_schemas_module=latest)
    finally:
        clean_versions(Path(__file__).parent / "schemas")


@pytest.fixture()
def testclient_2000(versioned_router: HeaderVersionedAPIRouter) -> TestClient:
    return TestClient(
        get_app(versioned_router),
        headers={"X-API-VERSION": "2000-01-01"},
    )


@pytest.fixture()
def testclient_2001(versioned_router: HeaderVersionedAPIRouter) -> TestClient:
    return TestClient(
        get_app(versioned_router),
        headers={"X-API-VERSION": "2001-01-01"},
    )


@pytest.fixture()
def testclient_2002(versioned_router: HeaderVersionedAPIRouter) -> TestClient:
    return TestClient(
        get_app(versioned_router),
        headers={"X-API-VERSION": "2002-01-01"},
    )


def test__2000(testclient_2000: TestClient):
    assert testclient_2000.get("/users/1").json() == {
        "id": 1,
        "address": "123 Example St",
    }

    assert testclient_2000.post(
        "/users",
        json={"name": "MyUser", "address": "123"},
    ).json() == {
        "id": 83,
        "address": "123",
    }


def test__2001(testclient_2001: TestClient):
    assert testclient_2001.get("/users/2").json() == {
        "id": 2,
        "addresses": ["123 Example St", "456 Main St"],
    }

    assert testclient_2001.post(
        "/users",
        json={"name": "MyUser", "addresses": ["124", "567"]},
    ).json() == {
        "id": 83,
        "addresses": ["124", "567"],
    }


def test__2002(testclient_2002: TestClient):
    assert testclient_2002.get("/users/7").json() == {"id": 7}

    assert testclient_2002.post("/users", json={"default_address": "123"}).json() == {
        "id": 83,
    }

    assert testclient_2002.get("/users/11/addresses").json() == {
        "data": [
            {"id": 83, "value": "123 Example St"},
            {"id": 91, "value": "456 Main St"},
        ],
    }
