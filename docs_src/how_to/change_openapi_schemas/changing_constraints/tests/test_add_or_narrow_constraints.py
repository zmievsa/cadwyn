from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = Path(__file__).parent.parent / "add_or_narrow_constraints"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


@pytest.mark.parametrize("version", ["2000-01-01", "2001-01-01"])
def test__create_user__with_250_character_name__should_create_user(
    client: TestClient, version: str
):
    response = client.post(
        "/users", json={"name": "a" * 250}, headers={"x-api-version": version}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "name": "a" * 250}


def test__create_user__in_2001_with_251_character_name__should_return_422(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "a" * 251},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["type"] == "string_too_long"


def test__create_user__in_2000_with_251_character_name__should_create_user(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "a" * 251},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "name": "a" * 251}


def test__openapi__in_2001__should_limit_names_in_requests_only(
    client: TestClient,
):
    schemas = client.get("/openapi.json?version=2001-01-01").json()[
        "components"
    ]["schemas"]

    assert (
        schemas["UserCreateRequest"]["properties"]["name"]["maxLength"] == 250
    )
    assert "maxLength" not in schemas["UserResource"]["properties"]["name"]


def test__openapi__in_2000__should_not_limit_names(client: TestClient):
    schemas = client.get("/openapi.json?version=2000-01-01").json()[
        "components"
    ]["schemas"]

    assert "maxLength" not in schemas["UserCreateRequest"]["properties"]["name"]
    assert "maxLength" not in schemas["UserResource"]["properties"]["name"]
