from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block010 import (
    AllowUsersToBelongToSeveralOrganizations,
    User,
)

router = VersionedAPIRouter()


@router.get("/users/me")
async def get_current_user() -> User:
    return User(name="John")


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AllowUsersToBelongToSeveralOrganizations),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_user_description(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    return openapi["components"]["schemas"]["User"]["description"]


def test__schema_had_description__openapi_of_older_version__should_have_old_description():
    assert (
        _get_user_description("2000-01-01")
        == "A user of the API who belongs to one organization."
    )


def test__schema_had_description__response_of_older_version__should_have_same_data():
    response = client.get("/users/me", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"name": "John"}


def test__schema_had_description__openapi_of_newer_version__should_have_docstring():
    assert (
        _get_user_description("2001-01-01")
        == "A user of the API who can belong to several organizations."
    )
