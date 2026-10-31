"""Test the router from block001 together with the version change from block002."""

from cadwyn import Cadwyn, Version, VersionBundle
from fastapi.testclient import TestClient

from docs_src.concepts.endpoint_migrations.block001 import router
from docs_src.concepts.endpoint_migrations.block002 import (
    RemoveGetUserByIdEndpoint,
)

app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", RemoveGetUserByIdEndpoint),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__endpoint_existed__request_to_older_version__should_reach_endpoint():
    response = client.get("/users/1", headers={"x-api-version": "2000-01-01"})
    assert response.status_code == 200, response.json()
    assert response.json() is None


def test__endpoint_existed__request_to_newer_version__should_return_404():
    response = client.get("/users/1", headers={"x-api-version": "2001-01-01"})
    assert response.status_code == 404


def test__endpoint_existed__openapi__should_only_document_endpoint_in_older_version():
    old_openapi = client.get("/openapi.json?version=2000-01-01").json()
    new_openapi = client.get("/openapi.json?version=2001-01-01").json()
    assert "/users/{user_id}" in old_openapi["paths"]
    assert "/users/{user_id}" not in new_openapi.get("paths", {})
