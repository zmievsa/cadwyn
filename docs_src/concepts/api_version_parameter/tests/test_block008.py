from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block008 import app

router = VersionedAPIRouter()


@router.get("/users")
async def list_users():
    return app.versions.api_version_var.get()


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__version_title_and_description__openapi__should_document_them_in_parameter():
    openapi = client.get("/openapi.json?version=2022-01-02").json()
    [parameter] = openapi["paths"]["/users"]["get"]["parameters"]
    assert parameter["schema"]["title"] == "My Great API version parameter"
    assert (
        parameter["description"]
        == "Description of my great API version parameter"
    )


def test__version_title_and_description__request__should_be_routed():
    response = client.get("/users", headers={"x-api-version": "2022-01-02"})
    assert response.status_code == 200, response.json()
    assert response.json() == "2022-01-02"
