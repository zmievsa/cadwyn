from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = Path(__file__).parent.parent / "required_with_compatible_default"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


def test__create_user__in_2000_without_country__should_default_to_usa(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "name": "John",
        "country": "USA",
    }


def test__create_user__in_2000_with_country__should_keep_the_country(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John", "country": "Canada"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["country"] == "Canada"


def test__create_user__in_2001_without_country__should_return_422(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["loc"] == ["body", "country"]


def test__create_user__in_2001_with_country__should_keep_the_country(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"name": "John", "country": "Canada"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "name": "John",
        "country": "Canada",
    }


def test__openapi__in_2000__should_show_usa_as_the_country_default(
    client: TestClient,
):
    schemas = client.get("/openapi.json?version=2000-01-01").json()[
        "components"
    ]["schemas"]

    assert schemas["UserCreateRequest"]["properties"]["country"]["default"] == (
        "USA"
    )
    assert schemas["UserCreateRequest"]["required"] == ["name"]


def test__openapi__in_2001__should_require_country(client: TestClient):
    schemas = client.get("/openapi.json?version=2001-01-01").json()[
        "components"
    ]["schemas"]

    assert (
        "default" not in schemas["UserCreateRequest"]["properties"]["country"]
    )
    assert schemas["UserCreateRequest"]["required"] == ["name", "country"]
