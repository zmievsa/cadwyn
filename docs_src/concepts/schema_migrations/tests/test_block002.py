from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block002 import AddFooToMySchema
from docs_src.concepts.schema_migrations.head.with_foo import MySchema

router = VersionedAPIRouter()


@router.get("/my_schema")
async def get_my_schema() -> MySchema:
    return MySchema(foo="foo")


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AddFooToMySchema),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_my_schema_openapi(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    return openapi["components"]["schemas"]["MySchema"]


def test__field_didnt_exist__openapi_of_older_version__should_not_have_field():
    assert "foo" not in _get_my_schema_openapi("2000-01-01")["properties"]


def test__field_didnt_exist__openapi_of_newer_version__should_have_field():
    assert _get_my_schema_openapi("2001-01-01")["properties"]["foo"] == {
        "type": "string",
        "title": "Foo",
    }


def test__field_didnt_exist__response_of_older_version__should_not_have_field():
    response = client.get("/my_schema", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {}


def test__field_didnt_exist__response_of_newer_version__should_have_field():
    response = client.get("/my_schema", headers={"x-api-version": "2001-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"foo": "foo"}
