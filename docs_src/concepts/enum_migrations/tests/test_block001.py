import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel

from docs_src.concepts.enum_migrations.block001 import (
    AddFooAndBarToMyEnum,
    MyEnum,
)


class Item(BaseModel):
    kind: MyEnum


received_items: list[Item] = []
router = VersionedAPIRouter()


@router.post("/items")
async def create_item(item: Item):
    received_items.append(item)


app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", AddFooAndBarToMyEnum),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


@pytest.mark.parametrize(
    ("version", "expected_members"),
    [("2022-11-16", ["baz"]), ("2023-02-10", ["baz", "foo", "bar"])],
)
def test__add_foo_and_bar_to_my_enum__in_openapi__should_document_them_only_in_new_version(
    version: str,
    expected_members: list[str],
):
    client = TestClient(app)

    openapi = client.get(f"/openapi.json?version={version}").json()

    assert (
        openapi["components"]["schemas"]["MyEnum"]["enum"] == expected_members
    )


@pytest.mark.parametrize("kind", ["foo", "bar"])
def test__add_foo_and_bar_to_my_enum__when_old_client_sends_new_member__should_return_422(
    kind: str,
):
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.post("/items", json={"kind": kind})

    assert response.status_code == 422, response.json()


@pytest.mark.parametrize(
    ("version", "kind"),
    [("2022-11-16", "baz"), ("2023-02-10", "baz"), ("2023-02-10", "foo")],
)
def test__add_foo_and_bar_to_my_enum__when_client_sends_member_of_its_version__should_give_head_enum_to_business_logic(
    version: str,
    kind: str,
):
    received_items.clear()
    client = TestClient(app, headers={"x-api-version": version})

    response = client.post("/items", json={"kind": kind})

    assert response.status_code == 200, response.json()
    assert received_items == [Item(kind=MyEnum(kind))]
