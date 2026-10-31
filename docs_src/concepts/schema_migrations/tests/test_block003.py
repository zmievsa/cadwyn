from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block003 import (
    ChangeMySchemaFooDescription,
)
from docs_src.concepts.schema_migrations.head.with_described_foo import MySchema

router = VersionedAPIRouter()


@router.get("/my_schema")
async def get_my_schema() -> MySchema:
    return MySchema(foo="foo")


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", ChangeMySchemaFooDescription),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_foo_openapi(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    return openapi["components"]["schemas"]["MySchema"]["properties"]["foo"]


def test__field_had__openapi_of_older_version__should_have_old_description():
    assert _get_foo_openapi("2000-01-01")["description"] == "Foo"


def test__field_had__openapi_of_newer_version__should_have_head_description():
    assert (
        _get_foo_openapi("2001-01-01")["description"]
        == "The value of my schema"
    )


def test__field_had__response_of_older_version__should_have_same_data():
    response = client.get("/my_schema", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"foo": "foo"}
