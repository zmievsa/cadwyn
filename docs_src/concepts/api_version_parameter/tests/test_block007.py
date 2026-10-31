import pytest
from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block007 import app

router = VersionedAPIRouter()


@router.get("/users")
async def list_users():
    return app.versions.api_version_var.get()


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


@pytest.mark.parametrize("version", ["v2", "anything_can_be_a_version", "v1"])
def test__string_version_format__request_with_existing_version__should_be_routed(
    version: str,
):
    response = client.get("/users", headers={"x-api-version": version})
    assert response.status_code == 200, response.json()
    assert response.json() == version


@pytest.mark.parametrize("version", ["v3", "2022-01-02"])
def test__string_version_format__request_with_unknown_version__should_return_404(
    version: str,
):
    response = client.get("/users", headers={"x-api-version": version})
    assert response.status_code == 404
