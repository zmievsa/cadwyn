import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from cadwyn.exceptions import CadwynHeadRequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from docs_src.concepts.enum_migrations.block002 import (
    MyEnum,
    RemoveFooAndBarFromMyEnum,
)


class Item(BaseModel):
    kind: MyEnum


router = VersionedAPIRouter()


@router.post("/items")
async def create_item(item: Item):
    return item


app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", RemoveFooAndBarFromMyEnum),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


@pytest.mark.parametrize(
    ("version", "expected_members"),
    [("2022-11-16", ["qux", "baz", "bar"]), ("2023-02-10", ["qux"])],
)
def test__remove_foo_and_bar_from_my_enum__in_openapi__should_document_them_only_in_old_version(
    version: str,
    expected_members: list[str],
):
    client = TestClient(app)

    openapi = client.get(f"/openapi.json?version={version}").json()

    assert (
        openapi["components"]["schemas"]["MyEnum"]["enum"] == expected_members
    )


@pytest.mark.parametrize("kind", ["baz", "bar"])
def test__remove_foo_and_bar_from_my_enum__when_new_client_sends_removed_member__should_return_422(
    kind: str,
):
    client = TestClient(app, headers={"x-api-version": "2023-02-10"})

    response = client.post("/items", json={"kind": kind})

    assert response.status_code == 422, response.json()


@pytest.mark.parametrize("kind", ["baz", "bar"])
def test__remove_foo_and_bar_from_my_enum__when_old_client_sends_removed_member__should_fail_to_convert_it_to_head(
    kind: str,
):
    # The old version accepts the member, but the request is still converted
    # to HEAD, which doesn't have it
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    with pytest.raises(CadwynHeadRequestValidationError):
        client.post("/items", json={"kind": kind})


def test__remove_foo_and_bar_from_my_enum__when_old_client_sends_kept_member__should_return_200():
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.post("/items", json={"kind": "qux"})

    assert response.status_code == 200, response.json()
    assert response.json() == {"kind": "qux"}
