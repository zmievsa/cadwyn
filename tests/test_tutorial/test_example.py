from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cadwyn import generate_code_for_versioned_packages

from .data import latest
from .routes import app, router
from .utils import clean_versions


@pytest.fixture(scope="module", autouse=True)
def _prepare_versioned_schemas():
    generate_code_for_versioned_packages(latest, app.versions)
    app.generate_and_include_versioned_routers(router)
    try:
        yield
    finally:
        clean_versions(Path(__file__).parent / "data")


@pytest.fixture()
def testclient_2000(_prepare_versioned_schemas: None) -> TestClient:
    return TestClient(app, headers={"X-API-VERSION": "2000-01-01"})


@pytest.fixture()
def testclient_2001(_prepare_versioned_schemas: None) -> TestClient:
    return TestClient(app, headers={"X-API-VERSION": "2001-01-01"})


@pytest.fixture()
def testclient_2002(_prepare_versioned_schemas: None) -> TestClient:
    return TestClient(app, headers={"X-API-VERSION": "2002-01-01"})


def test__2000(testclient_2000: TestClient):
    assert testclient_2000.get("/users/1").json() == {
        "id": 1,
        "address": "123 Example St",
    }

    assert testclient_2000.post("/users", json={"name": "MyUser", "address": "123"}).json() == {
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
