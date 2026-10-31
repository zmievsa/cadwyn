"""Test the router from block005 together with the version change from block006."""

from cadwyn import Cadwyn, Version, VersionBundle
from fastapi.testclient import TestClient

from docs_src.concepts.endpoint_migrations.block005 import router
from docs_src.concepts.endpoint_migrations.block006 import (
    UseParamsInsteadOfHeadersForUserNameFiltering,
)

app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", UseParamsInsteadOfHeadersForUserNameFiltering),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def _get_user_name_parameter(version: str):
    openapi = client.get(f"/openapi.json?version={version}").json()
    parameters = openapi["paths"]["/users"]["get"]["parameters"]
    [user_name] = [p for p in parameters if p["name"] != "x-api-version"]
    return user_name["name"], user_name["in"]


def test__endpoint_duplicates__openapi_of_older_version__should_take_user_name_from_header():
    assert _get_user_name_parameter("2000-01-01") == ("user-name", "header")


def test__endpoint_duplicates__openapi_of_newer_version__should_take_user_name_from_query():
    assert _get_user_name_parameter("2001-01-01") == ("user_name", "query")


def test__endpoint_duplicates__request_to_older_version_with_header__should_succeed():
    response = client.get(
        "/users",
        headers={"x-api-version": "2000-01-01", "user-name": "John"},
    )
    assert response.status_code == 200, response.json()


def test__endpoint_duplicates__request_to_newer_version_with_query__should_succeed():
    response = client.get(
        "/users",
        params={"user_name": "John"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()


def test__endpoint_duplicates__request_to_newer_version_with_header__should_return_422():
    response = client.get(
        "/users",
        headers={"x-api-version": "2001-01-01", "user-name": "John"},
    )
    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["loc"] == ["query", "user_name"]
