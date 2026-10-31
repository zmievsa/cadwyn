from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.endpoint_migrations.block004 import (
    ChangeGetUserByIdEndpointDescription,
)

router = VersionedAPIRouter()


@router.get("/users/{user_id}", description="My new description")
async def get_user(user_id: int):
    return {"id": user_id}


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", ChangeGetUserByIdEndpointDescription),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_description(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    return openapi["paths"]["/users/{user_id}"]["get"]["description"]


def test__endpoint_had_description__openapi_of_older_version__should_have_old_description():
    assert _get_description("2000-01-01") == "My old description"


def test__endpoint_had_description__openapi_of_newer_version__should_have_head_description():
    assert _get_description("2001-01-01") == "My new description"


def test__endpoint_had_description__request_to_older_version__should_reach_endpoint():
    response = client.get("/users/1", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": 1}
