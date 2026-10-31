from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block001 import RemoveFooFromMySchema
from docs_src.concepts.schema_migrations.head.without_foo import MySchema

router = VersionedAPIRouter()


@router.post("/my_schemas")
async def create_my_schema(payload: MySchema):
    return payload.model_dump()


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", RemoveFooFromMySchema),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_my_schema_openapi(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    return openapi["components"]["schemas"]["MySchema"]


def test__field_existed_as__openapi_of_older_version__should_have_field_with_type_and_description():
    schema = _get_my_schema_openapi("2000-01-01")
    assert schema["properties"]["foo"] == {
        "type": "array",
        "items": {"type": "string"},
        "title": "Foo",
        "description": "Foo",
    }
    assert schema["required"] == ["bar", "foo"]


def test__field_existed_as__openapi_of_newer_version__should_not_have_field():
    assert "foo" not in _get_my_schema_openapi("2001-01-01")["properties"]


def test__field_existed_as__request_to_older_version_without_field__should_return_422():
    response = client.post(
        "/my_schemas",
        json={"bar": "bar"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["loc"] == ["body", "foo"]


def test__field_existed_as__request_to_older_version_with_field__should_succeed():
    response = client.post(
        "/my_schemas",
        json={"bar": "bar", "foo": ["a", "b"]},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200, response.json()
    # HEAD does not have the field, so the endpoint does not get it
    assert response.json() == {"bar": "bar"}
