from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.endpoint_migrations.block003 import (
    AddGetCompanyByIdEndpoint,
)

router = VersionedAPIRouter()


@router.get("/companies/{company_id}")
async def get_company(company_id: int):
    return {"id": company_id}


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AddGetCompanyByIdEndpoint),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__endpoint_didnt_exist__request_to_older_version__should_return_404():
    response = client.get(
        "/companies/1", headers={"x-api-version": "2000-01-01"}
    )
    assert response.status_code == 404


def test__endpoint_didnt_exist__request_to_newer_version__should_reach_endpoint():
    response = client.get(
        "/companies/1", headers={"x-api-version": "2001-01-01"}
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"id": 1}
