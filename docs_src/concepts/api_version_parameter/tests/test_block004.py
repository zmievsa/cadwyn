from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block004 import app

router = VersionedAPIRouter()


@router.get("/users")
async def list_users():
    return app.versions.api_version_var.get()


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__custom_header_name__request_with_custom_header__should_be_routed():
    response = client.get("/users", headers={"MY-GREAT-HEADER": "2022-01-02"})
    assert response.status_code == 200, response.json()
    assert response.json() == "2022-01-02"


def test__custom_header_name__request_with_default_header__should_return_404():
    response = client.get("/users", headers={"x-api-version": "2022-01-02"})
    assert response.status_code == 404


def test__custom_header_name__openapi__should_document_custom_header():
    openapi = client.get("/openapi.json?version=2022-01-02").json()
    [parameter] = openapi["paths"]["/users"]["get"]["parameters"]
    assert parameter["name"] == "MY-GREAT-HEADER"
    assert parameter["in"] == "header"
