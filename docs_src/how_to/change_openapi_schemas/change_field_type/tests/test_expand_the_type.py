from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = Path(__file__).parent.parent / "expand_the_type"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


@pytest.mark.parametrize("role", ["admin", "regular", "moderator"])
def test__create_user__in_2001__should_return_the_role(
    client: TestClient, role: str
):
    response = client.post(
        "/users", json={"role": role}, headers={"x-api-version": "2001-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "role": role}


@pytest.mark.parametrize("role", ["admin", "regular"])
def test__create_user__in_2000_with_old_role__should_return_the_role(
    client: TestClient, role: str
):
    response = client.post(
        "/users", json={"role": role}, headers={"x-api-version": "2000-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "role": role}


def test__create_user__in_2000_with_moderator_role__should_return_422(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"role": "moderator"},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 422, response.json()


def test__get_user__in_2000_when_user_is_a_moderator__should_return_regular_role(
    client: TestClient,
):
    user_id = client.post(
        "/users",
        json={"role": "moderator"},
        headers={"x-api-version": "2001-01-01"},
    ).json()["id"]

    response = client.get(
        f"/users/{user_id}", headers={"x-api-version": "2000-01-01"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": user_id, "role": "regular"}


@pytest.mark.parametrize(
    ("version", "roles"),
    [
        ("2000-01-01", ["admin", "regular"]),
        ("2001-01-01", ["admin", "regular", "moderator"]),
    ],
)
def test__openapi__should_list_the_roles_of_the_version(
    client: TestClient, version: str, roles: list[str]
):
    schemas = client.get(f"/openapi.json?version={version}").json()[
        "components"
    ]["schemas"]

    assert schemas["UserRoleEnum"]["enum"] == roles
