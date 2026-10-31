from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block009 import (
    RenameOtherSchemaToMySchema,
)
from docs_src.concepts.schema_migrations.head.with_foo import MySchema

router = VersionedAPIRouter()


@router.get("/my_schema")
async def get_my_schema() -> MySchema:
    return MySchema(foo="foo")


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", RenameOtherSchemaToMySchema),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_openapi(version: str):
    return client.get(f"/openapi.json?version={version}").json()


def test__schema_had_name__openapi_of_older_version__should_use_old_name():
    openapi = _get_openapi("2000-01-01")
    schemas = openapi["components"]["schemas"]
    assert "OtherSchema" in schemas
    assert "MySchema" not in schemas
    responses = openapi["paths"]["/my_schema"]["get"]["responses"]
    assert responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OtherSchema"
    }


def test__schema_had_name__openapi_of_newer_version__should_use_head_name():
    schemas = _get_openapi("2001-01-01")["components"]["schemas"]
    assert "MySchema" in schemas
    assert "OtherSchema" not in schemas


def test__schema_had_name__response_of_older_version__should_have_same_data():
    response = client.get("/my_schema", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"foo": "foo"}
