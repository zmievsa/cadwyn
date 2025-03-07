import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from docs_src.quickstart.tutorial.block003 import app

    return TestClient(app)


def test__basic_post__with_version_2000(client: TestClient):
    response = client.post(
        "/users",
        json={"address": "123 Example St"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "address": "123 Example St"}

    user_id = response.json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2000-01-01"}
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "address": "123 Example St"}


def test__basic_post__with_version_2001(client: TestClient):
    response = client.post(
        "/users",
        json={"addresses": ["123 John St", "456 Smith St"]},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "addresses": ["123 John St", "456 Smith St"],
    }

    user_id = response.json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2001-01-01"}
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": user_id,
        "addresses": ["123 John St", "456 Smith St"],
    }
