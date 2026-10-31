from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = (
    Path(__file__).parent.parent / "required_with_incompatible_default"
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


@pytest.mark.parametrize(
    "body", [{"name": "John"}, {"name": "John", "phone": None}]
)
def test__create_user__in_2001_without_phone__should_return_422(
    client: TestClient, body: dict[str, str | None]
):
    response = client.post(
        "/users", json=body, headers={"x-api-version": "2001-01-01"}
    )

    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["loc"] == ["body", "phone"]


def test__create_user__in_2001_with_phone__should_return_the_phone(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John", "phone": "+15550100"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "name": "John",
        "phone": "+15550100",
    }


def test__create_user__in_2000_without_phone__should_create_user_without_phone(
    client: TestClient,
):
    response = client.post(
        "/users", json={"name": "John"}, headers={"x-api-version": "2000-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "name": "John", "phone": None}


def test__get_user__in_2001_when_created_in_2000_without_phone__should_return_null_phone(
    client: TestClient,
):
    user_id = client.post(
        "/users", json={"name": "John"}, headers={"x-api-version": "2000-01-01"}
    ).json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2001-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "name": "John", "phone": None}


def test__create_user__in_2000_with_phone__should_return_the_phone(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John", "phone": "+15550100"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["phone"] == "+15550100"


def test__openapi__in_2001__should_require_a_non_nullable_phone(
    client: TestClient,
):
    schemas = client.get("/openapi.json?version=2001-01-01").json()[
        "components"
    ]["schemas"]

    assert schemas["UserCreateRequest"]["properties"]["phone"] == {
        "type": "string",
        "title": "Phone",
    }
    assert schemas["UserCreateRequest"]["required"] == ["name", "phone"]


def test__openapi__in_2000__should_have_an_optional_nullable_phone(
    client: TestClient,
):
    schemas = client.get("/openapi.json?version=2000-01-01").json()[
        "components"
    ]["schemas"]

    assert schemas["UserCreateRequest"]["properties"]["phone"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "title": "Phone",
    }
    assert schemas["UserCreateRequest"]["required"] == ["name"]
