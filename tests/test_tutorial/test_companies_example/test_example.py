from datetime import date
from fastapi import FastAPI
from httpx import AsyncClient
import pytest

from universi import regenerate_dir_to_all_versions, api_version_var
from universi import VersionedAPIRouter, get_universi_dependency
from .companies import router, versions
from .schemas import latest
from fastapi.testclient import TestClient
from .utils import clean_versions


def get_app(router):
    app = FastAPI(dependencies=[get_universi_dependency(version_header_name="X-API-VERSION")])
    app.include_router(router)
    return app


@pytest.fixture(scope="module", autouse=True)
def routers() -> dict[date, VersionedAPIRouter]:
    regenerate_dir_to_all_versions(latest, versions)
    try:
        yield router.create_versioned_copies(versions, latest_schemas_module=latest)
    finally:
        clean_versions()


@pytest.fixture()
def testclient_2000(routers: dict[date, VersionedAPIRouter]) -> TestClient:
    return TestClient(get_app(routers[date(2000, 1, 1)]), headers={"X-API-VERSION": "2000-01-01"})


@pytest.fixture()
def testclient_2001(routers: dict[date, VersionedAPIRouter]) -> TestClient:
    return TestClient(get_app(routers[date(2001, 1, 1)]), headers={"X-API-VERSION": "2001-01-01"})


@pytest.fixture()
def testclient_2002(routers: dict[date, VersionedAPIRouter]) -> TestClient:
    return TestClient(get_app(routers[date(2002, 1, 1)]), headers={"X-API-VERSION": "2002-01-01"})


def test__2000(testclient_2000: TestClient):
    # insert_assert(testclient_2000.get("/companies/1").json())
    assert testclient_2000.get("/companies/1").json() == {
        "name": "Company 1",
        "vat_id": "First VAT ID",
    }
    # insert_assert(testclient_2000.post("/companies", json={"name": "MyCompany", "vat_id": "123"}).json())
    assert testclient_2000.post("/companies", json={"name": "MyCompany", "vat_id": "123"}).json() == {
        "name": "Company 1",
        "vat_id": "123",
    }


def test__2001(testclient_2001: TestClient):
    # insert_assert(testclient_2001.get("/companies/2").json())
    assert testclient_2001.get("/companies/1").json() == {
        "name": "Company 1",
        "vat_ids": ["First VAT ID", "Second VAT ID"],
    }
    # insert_assert(testclient_2001.post("/companies", json={"name": "MyCompany", "vat_ids": ["124"]}).json())
    assert testclient_2001.post("/companies", json={"name": "MyCompany", "vat_ids": ["124"]}).json() == {
        "name": "Company 1",
        "vat_ids": ["124", "First VAT ID", "Second VAT ID"],
    }


def test__2002(testclient_2002: TestClient):
    # insert_assert(testclient_2002.get("/companies/3").json())
    assert testclient_2002.get("/companies/1").json() == {"name": "Company 1"}
    # insert_assert(testclient_2002.post("/companies", json={"name": "MyCompany", "default_vat_id": "123"}).json())
    assert testclient_2002.post("/companies", json={"name": "MyCompany", "default_vat_id": "123"}).json() == {
        "name": "Company 1"
    }
