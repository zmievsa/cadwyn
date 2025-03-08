import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.version_with_path_and_numbers_instead_of_headers_and_dates.block001 import (
    app,
)


@pytest.fixture
def testclient() -> TestClient:
    return TestClient(app)


def test__v8(testclient: TestClient):
    response = testclient.post(
        "/v8/users", json={"address": "123 Example St"}
    ).json()
    assert response == {
        "id": IsUUID(4),
        "address": "123 Example St",
    }
    assert testclient.get(f"/v8/users/{response['id']}").json() == {
        "id": response["id"],
        "address": "123 Example St",
    }


def test__v9(testclient: TestClient):
    response = testclient.post(
        "/v9/users", json={"addresses": ["124", "567"]}
    ).json()
    assert response == {
        "id": IsUUID(4),
        "addresses": ["124", "567"],
    }

    assert testclient.get(f"/v9/users/{response['id']}").json() == {
        "id": response["id"],
        "addresses": ["124", "567"],
    }


def test__v10(testclient: TestClient):
    response = testclient.post(
        "/v10/users", json={"default_address": "wowee"}
    ).json()

    assert response == {
        "id": IsUUID(4),
    }

    assert testclient.get(f"/v10/users/{response['id']}").json() == {
        "id": response["id"]
    }

    assert testclient.get(f"/v10/users/{response['id']}/addresses").json() == {
        "data": [
            {"id": IsUUID(4), "value": "wowee"},
        ],
    }


def test__using_versions_not_present_in_versionbundle__waterfalling_shouldnt_work(
    testclient: TestClient,
):
    response = testclient.post("/v7/users", json={"address": "123 Example St"})
    assert response.status_code == 404

    response = testclient.post("/v11/users", json={"address": "123 Example St"})
    assert response.status_code == 404
