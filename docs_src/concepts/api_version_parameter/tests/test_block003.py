from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block003 import app

router = VersionedAPIRouter()


@router.get("/users")
async def list_users():
    return app.versions.api_version_var.get()


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__default_version_location__request_with_x_api_version_header__should_be_routed():
    response = client.get("/users", headers={"x-api-version": "2022-01-02"})
    assert response.status_code == 200, response.json()
    assert response.json() == "2022-01-02"


def test__default_version_location__request_without_version_header__should_return_404():
    assert client.get("/users").status_code == 404


def test__default_version_location__openapi__should_document_required_header():
    openapi = client.get("/openapi.json?version=2022-01-02").json()
    [parameter] = openapi["paths"]["/users"]["get"]["parameters"]
    assert parameter["name"] == "x-api-version"
    assert parameter["in"] == "header"
    assert parameter["required"] is True
