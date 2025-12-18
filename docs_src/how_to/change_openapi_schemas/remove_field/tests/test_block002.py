import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.remove_field.block002 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__includes_middle_name_in_request_and_response(
    testclient: TestClient,
):
    # Old version should support middle_name in both request and response
    response = testclient.post(
        "/users",
        json={"name": "John", "middle_name": "William"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "John",
        "middle_name": "William",
    }

    # Getting the user should also include middle_name
    user_response = testclient.get(
        f"/users/{response['id']}",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert user_response == {
        "id": response["id"],
        "name": "John",
        "middle_name": "William",
    }


def test__new_version__does_not_include_middle_name(testclient: TestClient):
    # New version should not include middle_name
    response = testclient.post(
        "/users",
        json={"name": "Jane"},
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Jane",
    }
    assert "middle_name" not in response


def test__old_version__middle_name_is_optional(testclient: TestClient):
    # Old version allows middle_name to be omitted (it's optional)
    response = testclient.post(
        "/users",
        json={"name": "Bob"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Bob",
        "middle_name": None,
    }


def test__cross_version__data_created_in_old_version_accessible_in_new(
    testclient: TestClient,
):
    # Create user with old version including middle_name
    response = testclient.post(
        "/users",
        json={"name": "Alice", "middle_name": "Marie"},
        headers={"x-api-version": "2000-01-01"},
    ).json()

    # Get with new version - middle_name should be excluded
    user_response = testclient.get(
        f"/users/{response['id']}",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert user_response == {
        "id": response["id"],
        "name": "Alice",
    }
    assert "middle_name" not in user_response
