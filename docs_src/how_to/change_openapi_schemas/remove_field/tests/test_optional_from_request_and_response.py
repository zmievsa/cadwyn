from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = (
    Path(__file__).parent.parent / "optional_from_request_and_response"
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


def test__create_user__in_2000_with_middle_name__should_return_middle_name(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John", "middle_name": "Fitzgerald"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "name": "John",
        "middle_name": "Fitzgerald",
    }


def test__create_user__in_2000_without_middle_name__should_return_null_middle_name(
    client: TestClient,
):
    response = client.post(
        "/users", json={"name": "John"}, headers={"x-api-version": "2000-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "name": "John",
        "middle_name": None,
    }


def test__create_user__in_2001_with_middle_name__should_ignore_middle_name(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John", "middle_name": "Fitzgerald"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "name": "John"}

    response = client.get(
        f"/users/{response.json()['id']}",
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.json()["middle_name"] is None


def test__get_user__in_2001_when_created_in_2000_with_middle_name__should_not_return_middle_name(
    client: TestClient,
):
    user_id = client.post(
        "/users",
        json={"name": "John", "middle_name": "Fitzgerald"},
        headers={"x-api-version": "2000-01-01"},
    ).json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2001-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "name": "John"}


def test__openapi__in_2000__should_have_middle_name(client: TestClient):
    schemas = client.get("/openapi.json?version=2000-01-01").json()[
        "components"
    ]["schemas"]

    for schema_name in ("UserCreateRequest", "UserResource"):
        assert schemas[schema_name]["properties"]["middle_name"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "title": "Middle Name",
            "description": "User's Middle Name",
        }


def test__openapi__in_2001__should_not_have_middle_name(client: TestClient):
    schemas = client.get("/openapi.json?version=2001-01-01").json()[
        "components"
    ]["schemas"]

    for schema_name in ("UserCreateRequest", "UserResource"):
        assert "middle_name" not in schemas[schema_name]["properties"]
