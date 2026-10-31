import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block006 import (
    AllowLongBusinessInvoiceCodes,
    BusinessInvoice,
    Invoice,
    PersonalInvoice,
)

router = VersionedAPIRouter()


@router.post("/invoices")
async def create_invoice(invoice: Invoice) -> Invoice:
    return invoice


@router.post("/business_invoices")
async def create_business_invoice(invoice: BusinessInvoice) -> BusinessInvoice:
    return invoice


@router.post("/personal_invoices")
async def create_personal_invoice(invoice: PersonalInvoice) -> PersonalInvoice:
    return invoice


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AllowLongBusinessInvoiceCodes),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
client = TestClient(app)


def test__inherited_field_had__long_business_invoice_code_in_older_version__should_return_422():
    response = client.post(
        "/business_invoices",
        json={"code": "123456"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["type"] == "string_too_long"


@pytest.mark.parametrize("path", ["/invoices", "/personal_invoices"])
def test__inherited_field_had__long_parent_or_sibling_code_in_older_version__should_succeed(
    path: str,
):
    response = client.post(
        path,
        json={"code": "123456"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"code": "123456"}


@pytest.mark.parametrize(
    "path", ["/invoices", "/business_invoices", "/personal_invoices"]
)
def test__inherited_field_had__long_code_in_newer_version__should_succeed(
    path: str,
):
    response = client.post(
        path,
        json={"code": "123456"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"code": "123456"}


def test__inherited_field_had__openapi_of_older_version__should_only_limit_business_invoice_code():
    openapi = client.get("/openapi.json?version=2000-01-01").json()
    schemas = openapi["components"]["schemas"]
    assert schemas["BusinessInvoice"]["properties"]["code"]["maxLength"] == 5
    assert "maxLength" not in schemas["Invoice"]["properties"]["code"]
    assert "maxLength" not in schemas["PersonalInvoice"]["properties"]["code"]
