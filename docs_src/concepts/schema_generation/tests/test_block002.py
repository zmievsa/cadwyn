from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_generation.block002 import Item, Page, app

router = VersionedAPIRouter()


@router.get("/items")
async def list_items() -> Page[Item]:
    return Page[Item](items=[Item(full_name="John")])


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_schemas(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    return openapi["components"]["schemas"]


def test__generic_model__openapi_of_older_version__should_use_old_type_arg():
    schemas = _get_schemas("2000-01-01")
    assert schemas["Page_Item_"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert set(schemas["Item"]["properties"]) == {"name"}
    assert [name for name in schemas if name.startswith("Item")] == ["Item"]


def test__generic_model__openapi_of_newer_version__should_use_head_type_arg():
    schemas = _get_schemas("2001-01-01")
    assert schemas["Page_Item_"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert set(schemas["Item"]["properties"]) == {"full_name"}


def test__generic_model__response_of_newer_version__should_use_head_type_arg():
    response = client.get("/items", headers={"x-api-version": "2001-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"items": [{"full_name": "John"}]}
