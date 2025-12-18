import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.add_field.block001 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__country_defaults_to_usa(testclient: TestClient):
    # Old version has default "USA" for country
    response = testclient.post(
        "/users",
        json={"name": "John"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "John",
        "country": "USA",
    }


def test__old_version__country_can_be_provided(testclient: TestClient):
    # Old version allows specifying country explicitly
    response = testclient.post(
        "/users",
        json={"name": "Alice", "country": "Canada"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Alice",
        "country": "Canada",
    }


def test__new_version__country_is_required(testclient: TestClient):
    # New version requires country field
    response = testclient.post(
        "/users",
        json={"name": "Jane"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 422  # Validation error - country is required


def test__new_version__works_with_country_provided(testclient: TestClient):
    # New version works when country is provided
    response = testclient.post(
        "/users",
        json={"name": "Jane", "country": "UK"},
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Jane",
        "country": "UK",
    }
