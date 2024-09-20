import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from docs_src.quickstart.tutorial.block001 import app

    return TestClient(app)


def test__basic_post__with_version_2000(client: TestClient):
    response = client.post("/users", json={"address": "123 Example St"}, headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "address": "123 Example St"}

    user_id = response.json()["id"]

    response = client.get(f"/users/{user_id}", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "address": "123 Example St"}
