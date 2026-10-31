import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi import HTTPException
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.block007 import (
    ReplaceInvoiceNotFoundStatusCode400With404,
)

router = VersionedAPIRouter()


@router.get("/v1/invoices")
async def get_invoice(invoice_id: int):
    if invoice_id != 1:
        raise HTTPException(404, "Invoice not found")
    return {"id": invoice_id}


app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", ReplaceInvoiceNotFoundStatusCode400With404),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


@pytest.mark.parametrize(
    ("version", "expected_status_code"),
    [("2022-11-16", 400), ("2023-02-10", 404)],
)
def test__replace_invoice_not_found_status_code__when_invoice_is_missing__should_return_the_status_code_of_the_version(
    version: str,
    expected_status_code: int,
):
    client = TestClient(app, headers={"x-api-version": version})

    response = client.get("/v1/invoices", params={"invoice_id": 2})

    assert response.status_code == expected_status_code, response.json()
    assert response.json() == {"detail": "Invoice not found"}


@pytest.mark.parametrize("version", ["2022-11-16", "2023-02-10"])
def test__replace_invoice_not_found_status_code__when_invoice_exists__should_return_200(
    version: str,
):
    client = TestClient(app, headers={"x-api-version": version})

    response = client.get("/v1/invoices", params={"invoice_id": 1})

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": 1}
