import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.change_field_type.block001 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__does_not_support_moderator_role(testclient: TestClient):
    # Old version doesn't have moderator role - should fail validation
    response = testclient.post(
        "/users?name=John&role=moderator",
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 422


def test__old_version__supports_admin_and_regular_roles(testclient: TestClient):
    # Old version supports admin role
    response = testclient.post(
        "/users?name=John&role=admin",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "John",
        "role": "admin",
    }

    # Old version supports regular role
    response = testclient.post(
        "/users?name=Jane&role=regular",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Jane",
        "role": "regular",
    }


def test__new_version__supports_moderator_role(testclient: TestClient):
    # New version supports moderator role
    response = testclient.post(
        "/users?name=Alice&role=moderator",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "name": "Alice",
        "role": "moderator",
    }


def test__cross_version__moderator_converts_to_regular_in_old_version(
    testclient: TestClient,
):
    # Create moderator in new version
    response_new = testclient.post(
        "/users?name=Bob&role=moderator",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response_new["role"] == "moderator"

    # Get with old version - moderator should be converted to regular
    response_old = testclient.get(
        f"/users/{response_new['id']}",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response_old == {
        "id": response_new["id"],
        "name": "Bob",
        "role": "regular",
    }
