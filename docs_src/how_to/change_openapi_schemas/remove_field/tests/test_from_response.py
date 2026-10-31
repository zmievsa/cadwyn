from collections.abc import Iterator
from pathlib import Path

import pytest
from dirty_equals import IsUUID
from fastapi.testclient import TestClient

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLE_DIR = Path(__file__).parent.parent / "from_response"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with example_project_client(EXAMPLE_DIR) as client:
        yield client


@pytest.mark.parametrize(
    ("date_of_birth", "zodiac_sign"),
    [
        ("2000-01-05", "Capricorn"),
        ("2000-03-21", "Aries"),
        ("2000-12-25", "Capricorn"),
    ],
)
def test__create_user__in_2000__should_return_zodiac_sign(
    client: TestClient, date_of_birth: str, zodiac_sign: str
):
    response = client.post(
        "/users",
        json={"date_of_birth": date_of_birth},
        headers={"x-api-version": "2000-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": IsUUID(4),
        "date_of_birth": date_of_birth,
        "zodiac_sign": zodiac_sign,
    }


def test__create_user__in_2001__should_not_return_zodiac_sign(
    client: TestClient,
):
    response = client.post(
        "/users",
        json={"date_of_birth": "2000-03-21"},
        headers={"x-api-version": "2001-01-01"},
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": IsUUID(4), "date_of_birth": "2000-03-21"}


def test__openapi__in_2000__should_have_zodiac_sign(client: TestClient):
    schemas = client.get("/openapi.json?version=2000-01-01").json()[
        "components"
    ]["schemas"]

    assert schemas["UserResource"]["properties"]["zodiac_sign"] == {
        "type": "string",
        "title": "Zodiac Sign",
        "description": "User's magical sign",
    }
    assert "zodiac_sign" in schemas["UserResource"]["required"]


def test__openapi__in_2001__should_not_have_zodiac_sign(client: TestClient):
    schemas = client.get("/openapi.json?version=2001-01-01").json()[
        "components"
    ]["schemas"]

    assert "zodiac_sign" not in schemas["UserResource"]["properties"]
