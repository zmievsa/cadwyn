import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.change_field_type.block002 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__accepts_datetime(testclient: TestClient):
    # Old version accepts datetime for date_of_birth
    response = testclient.post(
        "/users",
        json={"name": "John", "date_of_birth": "1990-04-15T10:30:00"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "John",
        "date_of_birth": "1990-04-15T10:30:00",
    }


def test__new_version__accepts_date(testclient: TestClient):
    # New version uses date (not datetime) for date_of_birth
    response = testclient.post(
        "/users",
        json={"name": "Jane", "date_of_birth": "1990-04-15"},
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Jane",
        "date_of_birth": "1990-04-15",
    }


def test__new_version__accepts_datetime_with_zero_time(testclient: TestClient):
    # New version accepts datetime strings that have zero time (midnight)
    response = testclient.post(
        "/users",
        json={"name": "Alice", "date_of_birth": "1990-04-15T00:00:00"},
        headers={"x-api-version": "2001-01-01"},
    ).json()
    # The datetime is converted to date
    assert response == {
        "id": IsUUID(4),
        "name": "Alice",
        "date_of_birth": "1990-04-15",
    }


def test__cross_version__date_created_in_new_version_readable_in_old(
    testclient: TestClient,
):
    # Create with new version (date)
    response_new = testclient.post(
        "/users",
        json={"name": "Bob", "date_of_birth": "1990-08-10"},
        headers={"x-api-version": "2001-01-01"},
    ).json()

    # Get with old version - should be returned as datetime
    response_old = testclient.get(
        f"/users/{response_new['id']}",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    # Old version should return datetime format (with time component)
    assert response_old["id"] == response_new["id"]
    assert response_old["name"] == "Bob"
    # The date should be preserved, time will be 00:00:00
    assert response_old["date_of_birth"].startswith("1990-08-10")
