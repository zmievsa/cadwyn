from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = Path(__file__).parent.parent / "narrow_the_type"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


def test__create_user__in_2001_with_date__should_return_date(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"date_of_birth": "2000-05-17"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "date_of_birth": "2000-05-17"}


def test__create_user__in_2001_with_datetime__should_return_422(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"date_of_birth": "2000-05-17T10:20:30"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 422, response.json()


def test__create_user__in_2000_with_datetime__should_return_datetime(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"date_of_birth": "2000-05-17T10:20:30"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "date_of_birth": "2000-05-17T10:20:30",
    }


def test__get_user__in_2001_when_created_in_2000_with_datetime__should_return_date(
    client: TestClient,
):
    user_id = client.post(
        "/users",
        json={"date_of_birth": "2000-05-17T10:20:30"},
        headers={"x-api-version": "2000-01-01"},
    ).json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2001-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "date_of_birth": "2000-05-17"}


def test__get_user__in_2000_when_created_in_2001_with_date__should_return_midnight_datetime(
    client: TestClient,
):
    user_id = client.post(
        "/users",
        json={"date_of_birth": "2000-05-17"},
        headers={"x-api-version": "2001-01-01"},
    ).json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2000-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": user_id,
        "date_of_birth": "2000-05-17T00:00:00",
    }


@pytest.mark.parametrize(
    ("version", "format_"),
    [("2000-01-01", "date-time"), ("2001-01-01", "date")],
)
def test__openapi__should_have_the_date_of_birth_format_of_the_version(
    client: TestClient, version: str, format_: str
):
    schemas = client.get(f"/openapi.json?version={version}").json()[
        "components"
    ]["schemas"]

    for schema_name in ("UserCreateRequest", "UserResource"):
        date_of_birth = schemas[schema_name]["properties"]["date_of_birth"]
        assert date_of_birth["format"] == format_
