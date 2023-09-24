from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from universi import (
    VersionedAPIRouter,
    get_universi_dependency,
    regenerate_dir_to_all_versions,
)
from universi.routing import generate_all_router_versions

from ..utils import clean_versions
from .schemas import latest
from .users import api_version_var, router, versions


def get_app(router: APIRouter) -> FastAPI:
    app = FastAPI(
        dependencies=[get_universi_dependency(version_header_name="X-API-VERSION", api_version_var=api_version_var)],
    )
    app.include_router(router)
    return app


@pytest.fixture(scope="module", autouse=True)
def routers() -> Generator[dict[date, APIRouter], None, None]:
    regenerate_dir_to_all_versions(latest, versions)
    try:
        yield generate_all_router_versions(router, versions=versions, latest_schemas_module=latest)
    finally:
        clean_versions(Path(__file__).parent / "schemas")


@pytest.fixture()
def testclient_2000(routers: dict[date, VersionedAPIRouter]) -> TestClient:
    return TestClient(
        get_app(routers[date(2000, 1, 1)]),
        headers={"X-API-VERSION": "2000-01-01"},
    )


@pytest.fixture()
def testclient_2001(routers: dict[date, VersionedAPIRouter]) -> TestClient:
    return TestClient(
        get_app(routers[date(2001, 1, 1)]),
        headers={"X-API-VERSION": "2001-01-01"},
    )


@pytest.fixture()
def testclient_2002(routers: dict[date, VersionedAPIRouter]) -> TestClient:
    return TestClient(
        get_app(routers[date(2002, 1, 1)]),
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
