from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block005 import app

router = VersionedAPIRouter()


@router.get("/{api_version}/users")
async def list_users():
    return app.versions.api_version_var.get()


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__path_version_location__request_with_version_in_path__should_be_routed():
    response = client.get("/2022-01-02/users")
    assert response.status_code == 200, response.json()
    assert response.json() == "2022-01-02"


def test__path_version_location__request_with_version_header__should_return_404():
    response = client.get("/users", headers={"x-api-version": "2022-01-02"})
    assert response.status_code == 404


def test__path_version_location__openapi__should_document_path_parameter():
    openapi = client.get("/openapi.json?version=2022-01-02").json()
    [parameter] = openapi["paths"]["/{api_version}/users"]["get"]["parameters"]
    assert parameter["name"] == "api_version"
    assert parameter["in"] == "path"
