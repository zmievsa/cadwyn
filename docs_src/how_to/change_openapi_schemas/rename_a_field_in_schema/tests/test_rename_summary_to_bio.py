from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = Path(__file__).parent.parent / "rename_summary_to_bio"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


def test__create_user__in_2000_with_summary__should_return_summary(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"summary": "Likes cats"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "summary": "Likes cats"}


def test__create_user__in_2001_with_bio__should_return_bio(client: TestClient):
    response = client.post(
        "/users",
        json={"bio": "Likes cats"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "bio": "Likes cats"}


def test__get_user__in_2001_when_created_in_2000__should_return_bio(
    client: TestClient,
):
    user_id = client.post(
        "/users",
        json={"summary": "Likes cats"},
        headers={"x-api-version": "2000-01-01"},
    ).json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2001-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "bio": "Likes cats"}


@pytest.mark.parametrize(
    ("version", "field_name", "missing_field_name"),
    [("2000-01-01", "summary", "bio"), ("2001-01-01", "bio", "summary")],
)
def test__openapi__should_have_the_field_name_of_the_version(
    client: TestClient, version: str, field_name: str, missing_field_name: str
):
    schemas = client.get(f"/openapi.json?version={version}").json()[
        "components"
    ]["schemas"]

    for schema_name in ("UserCreateRequest", "UserResource"):
        assert field_name in schemas[schema_name]["properties"]
        assert missing_field_name not in schemas[schema_name]["properties"]
