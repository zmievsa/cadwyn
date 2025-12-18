import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.changing_constraints.block001 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__allows_long_names(testclient: TestClient):
    # Old version has no max_length constraint
    long_name = "A" * 300
    response = testclient.post(
        "/users",
        json={"name": long_name},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": IsUUID(4),
        "name": long_name,
    }


def test__latest_version__rejects_long_names(testclient: TestClient):
    # Latest version has max_length=250 constraint
    long_name = "A" * 300
    response = testclient.post(
        "/users",
        json={"name": long_name},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 422  # Validation error


def test__latest_version__allows_valid_names(testclient: TestClient):
    # Latest version allows names up to 250 characters
    valid_name = "A" * 250
    response = testclient.post(
        "/users",
        json={"name": valid_name},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": IsUUID(4),
        "name": valid_name,
    }
