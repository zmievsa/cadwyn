from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from universi import (
    VersionedAPIRouter,
    get_universi_dependency,
    regenerate_dir_to_all_versions,
)

from ..utils import clean_versions
from .schemas import latest
from .users import router, versions


def get_app(router):
    app = FastAPI(
        dependencies=[get_universi_dependency(version_header_name="X-API-VERSION")],
    )
    app.include_router(router)
    return app


@pytest.fixture(scope="module", autouse=True)
def routers() -> Generator[dict[date, VersionedAPIRouter], None, None]:
    regenerate_dir_to_all_versions(latest, versions)
    try:
        yield router.create_versioned_copies(versions, latest_schemas_module=latest)
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


def test__2000(testclient_2000: TestClient):
    # insert_assert(testclient_2000.get("/users/1").json())
    assert testclient_2000.get("/users/1").json() == {
        "id": 1,
        "address": "123 Example St",
    }
    # insert_assert(testclient_2000.post("/users", json={"name": "MyUser", "address": "123"}).json())
    assert testclient_2000.post(
        "/users",
        json={"name": "MyUser", "address": "123"},
    ).json() == {
        "id": 83,
        "address": "123",
    }


def test__2001(testclient_2001: TestClient):
    # insert_assert(testclient_2001.get("/users/2").json())
    assert testclient_2001.get("/users/2").json() == {
        "id": 2,
        "addresses": ["123 Example St", "456 Main St"],
    }
    # insert_assert(testclient_2001.post("/users", json={"name": "MyUser", "addresses": ["124"]}).json())
    assert testclient_2001.post(
        "/users",
        json={"name": "MyUser", "addresses": ["124"]},
    ).json() == {
        "id": 83,
        "addresses": ["124"],
    }
