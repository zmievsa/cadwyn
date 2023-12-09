from pathlib import Path

import pytest
from dirty_equals import IsUUID
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
    response = testclient_2000.post("/users", json={"address": "123 Example St"}).json()
    assert response == {
        "id": IsUUID(4),
        "address": "123 Example St",
    }
    assert testclient_2000.get(f"/users/{response['id']}").json() == {
        "id": response["id"],
        "address": "123 Example St",
    }


def test__2001(testclient_2001: TestClient):
    response = testclient_2001.post("/users", json={"addresses": ["124", "567"]}).json()
    assert response == {
        "id": IsUUID(4),
        "addresses": ["124", "567"],
    }

    assert testclient_2001.get(f"/users/{response['id']}").json() == {
        "id": response["id"],
        "addresses": ["124", "567"],
    }


def test__2002(testclient_2002: TestClient):
    response = testclient_2002.post("/users", json={"default_address": "wowee"}).json()

    assert response == {
        "id": IsUUID(4),
    }

    assert testclient_2002.get(f"/users/{response['id']}").json() == {"id": response["id"]}

    assert testclient_2002.get(f"/users/{response['id']}/addresses").json() == {
        "data": [
            {"id": IsUUID(4), "value": "wowee"},
        ],
    }
