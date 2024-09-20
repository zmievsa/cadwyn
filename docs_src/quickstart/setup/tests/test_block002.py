import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from docs_src.quickstart.setup.block002 import app

    return TestClient(app)


def test__basic_get(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200, response.json()
    assert response.json() == {"message": "Hello World"}


def test__basic_get__with_version(client: TestClient):
    response = client.get("/", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 404, response.json()
