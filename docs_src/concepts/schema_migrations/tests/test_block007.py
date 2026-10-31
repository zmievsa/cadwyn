from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block007 import (
    RemoveFooValidatorFromMySchema,
)
from docs_src.concepts.schema_migrations.head.with_foo import MySchema

router = VersionedAPIRouter()


@router.post("/my_schemas")
async def create_my_schema(payload: MySchema) -> MySchema:
    return payload


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", RemoveFooValidatorFromMySchema),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__validator_existed__value_without_colon_in_older_version__should_return_422():
    response = client.post(
        "/my_schemas",
        json={"foo": "foo"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 422, response.json()
    assert (
        response.json()["detail"][0]["msg"]
        == "Value error, foo must contain a colon"
    )


def test__validator_existed__value_with_colon_in_older_version__should_succeed():
    response = client.post(
        "/my_schemas",
        json={"foo": "a:b"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"foo": "a:b"}


def test__validator_existed__value_without_colon_in_newer_version__should_succeed():
    response = client.post(
        "/my_schemas",
        json={"foo": "foo"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"foo": "foo"}
