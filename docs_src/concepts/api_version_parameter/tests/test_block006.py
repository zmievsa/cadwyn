from cadwyn import VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block006 import app

router = VersionedAPIRouter()


@router.get("/users")
async def list_users():
    return app.versions.api_version_var.get()


app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__date_version_format__request_with_exact_version__should_be_routed():
    response = client.get("/users", headers={"x-api-version": "2022-01-02"})
    assert response.status_code == 200, response.json()
    assert response.json() == "2022-01-02"


def test__date_version_format__request_with_later_date__should_waterfall_to_closest_earlier_version():
    response = client.get("/users", headers={"x-api-version": "2022-01-03"})
    assert response.status_code == 200, response.json()
    assert response.json() == "2022-01-02"


def test__date_version_format__request_with_earlier_date__should_return_404():
    response = client.get("/users", headers={"x-api-version": "2022-01-01"})
    assert response.status_code == 404


def test__date_version_format__request_with_arbitrary_string__should_return_422():
    response = client.get("/users", headers={"x-api-version": "v1"})
    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["loc"] == ["header", "x-api-version"]
