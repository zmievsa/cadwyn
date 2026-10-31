import pytest
from cadwyn import Cadwyn, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.block002 import versions

router = VersionedAPIRouter()


@router.only_exists_in_older_versions
@router.get("/v1/tax_ids")
async def list_tax_ids():
    return [{"id": "tax_1"}]


@router.only_exists_in_older_versions
@router.post("/v1/tax_ids")
async def create_tax_id():
    return {"id": "tax_2"}


app = Cadwyn(versions=versions)
app.generate_and_include_versioned_routers(router)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test__remove_tax_id_endpoints__in_version_before_2023_02_10__should_serve_the_endpoints(
    method: str,
):
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.request(method, "/v1/tax_ids")

    assert response.status_code == 200, response.json()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test__remove_tax_id_endpoints__in_version_2023_02_10__should_return_404(
    method: str,
):
    client = TestClient(app, headers={"x-api-version": "2023-02-10"})

    response = client.request(method, "/v1/tax_ids")

    assert response.status_code == 404, response.json()


def test__remove_tax_id_endpoints__in_openapi__should_document_the_endpoints_only_before_2023_02_10():
    client = TestClient(app)

    old_paths = client.get("/openapi.json?version=2022-11-16").json()["paths"]
    new_paths = client.get("/openapi.json?version=2023-02-10").json()["paths"]

    assert set(old_paths["/v1/tax_ids"]) == {"get", "post"}
    assert "/v1/tax_ids" not in new_paths
