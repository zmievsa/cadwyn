import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.add_field.block002 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__phone_is_optional(testclient: TestClient):
    # Old version has phone as optional (nullable with default=None)
    response = testclient.post(
        "/users",
        json={"name": "John"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "John",
        "phone": None,
    }


def test__old_version__phone_can_be_provided(testclient: TestClient):
    # Old version allows specifying phone explicitly
    response = testclient.post(
        "/users",
        json={"name": "Alice", "phone": "+1234567890"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Alice",
        "phone": "+1234567890",
    }


def test__new_version__phone_is_required(testclient: TestClient):
    # New version requires phone field (non-nullable, no default)
    response = testclient.post(
        "/users",
        json={"name": "Jane"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 422  # Validation error - phone is required


def test__new_version__works_with_phone_provided(testclient: TestClient):
    # New version works when phone is provided
    response = testclient.post(
        "/users",
        json={"name": "Jane", "phone": "+9876543210"},
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Jane",
        "phone": "+9876543210",
    }
