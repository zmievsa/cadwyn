from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block005 import MySchema, RenameFooKey

router = VersionedAPIRouter()


@router.post("/my_schema")
async def echo_my_schema(my_schema: MySchema) -> MySchema:
    return my_schema


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", RenameFooKey),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_openapi_keys(version: str) -> list[str]:
    openapi = client.get(f"/openapi.json?version={version}").json()
    return list(openapi["components"]["schemas"]["MySchema"]["properties"])


def _post(version: str, body: dict[str, str]):
    return client.post(
        "/my_schema", json=body, headers={"x-api-version": version}
    )


def test__alias_change__older_version__should_use_old_key():
    assert _get_openapi_keys("2000-01-01") == ["old_foo_key"]
    response = _post("2000-01-01", {"old_foo_key": "foo"})
    assert (response.status_code, response.json()) == (
        200,
        {"old_foo_key": "foo"},
    )


def test__alias_change__newer_version__should_use_head_key():
    assert _get_openapi_keys("2001-01-01") == ["foo_key"]
    response = _post("2001-01-01", {"foo_key": "foo"})
    assert (response.status_code, response.json()) == (200, {"foo_key": "foo"})


def test__alias_change__older_version_gets_new_key__should_return_422():
    response = _post("2000-01-01", {"foo_key": "foo"})
    assert response.status_code == 422
    message = (
        'The key "foo_key" of "MySchema.foo" was renamed to "old_foo_key" in this '
        "API version"
    )
    assert [error["msg"] for error in response.json()["detail"]] == [message]
