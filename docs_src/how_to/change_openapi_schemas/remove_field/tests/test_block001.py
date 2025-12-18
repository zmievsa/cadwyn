import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.remove_field.block001 import (
    app,
    database_parody,
)


@pytest.fixture
def testclient() -> TestClient:
    database_parody.clear()
    return TestClient(app)


def test__old_version__includes_zodiac_sign(testclient: TestClient):
    # Old version should include zodiac_sign field
    response = testclient.post(
        "/users?date_of_birth=1990-04-15",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "date_of_birth": "1990-04-15",
        "zodiac_sign": "aries",
    }


def test__new_version__does_not_include_zodiac_sign(testclient: TestClient):
    # New version should not include zodiac_sign field
    response = testclient.post(
        "/users?date_of_birth=1990-04-15",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert response == {
        "id": IsUUID(4),
        "date_of_birth": "1990-04-15",
    }
    assert "zodiac_sign" not in response


def test__cross_version__data_created_in_old_version_accessible_in_both(
    testclient: TestClient,
):
    # Create with old version
    response_old = testclient.post(
        "/users?date_of_birth=1990-08-10",
        headers={"x-api-version": "2000-01-01"},
    ).json()
    assert "zodiac_sign" in response_old
    assert response_old["zodiac_sign"] == "leo"

    # Get with new version - zodiac_sign should be excluded
    response_new = testclient.get(
        f"/users/{response_old['id']}",
        headers={"x-api-version": "2001-01-01"},
    ).json()
    assert "zodiac_sign" not in response_new
    assert response_new == {
        "id": response_old["id"],
        "date_of_birth": "1990-08-10",
    }
