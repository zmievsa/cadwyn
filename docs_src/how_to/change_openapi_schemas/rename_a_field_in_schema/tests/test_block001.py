import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.rename_a_field_in_schema.block001 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__uses_summary_field(testclient: TestClient):
    # Old version uses "summary" field name
    response = testclient.post(
        "/users",
        json={"summary": "Original summary text"},
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "summary": "Original summary text",
    }

    # Getting the user should also return "summary" field
    user_response = testclient.get(
        f"/users/{response['id']}",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert user_response == {
        "id": response["id"],
        "summary": "Original summary text",
    }


def test__new_version__uses_bio_field(testclient: TestClient):
    # New version uses "bio" field name
    response = testclient.post(
        "/users",
        json={"bio": "My awesome bio"},
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "bio": "My awesome bio",
    }

    # Getting the user should also return "bio" field
    user_response = testclient.get(
        f"/users/{response['id']}",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert user_response == {
        "id": response["id"],
        "bio": "My awesome bio",
    }


def test__cross_version__data_created_in_old_version_readable_in_new(
    testclient: TestClient,
):
    # Create user with old version
    response = testclient.post(
        "/users",
        json={"summary": "Cross version text"},
        headers={"x-api-version": "2000-01-01"},
    ).json()

    # Read with new version - should use "bio" field
    user_response = testclient.get(
        f"/users/{response['id']}",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert user_response == {
        "id": response["id"],
        "bio": "Cross version text",
    }
