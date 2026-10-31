import sys

import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.fake_modules import invoices
from docs_src.concepts.version_changes.fake_modules.invoices import (
    InvoiceCreateRequest,
    InvoiceResource,
)

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "invoices", invoices)
    from docs_src.concepts.version_changes.block006 import (
        RenameInvoiceCreationDateToCreatedAt,
    )

database: list[InvoiceCreateRequest] = []
router = VersionedAPIRouter()


@router.post("/v1/invoices")
async def create_invoice(invoice: InvoiceCreateRequest):
    database.append(invoice)
    return {"id": len(database)}


@router.get("/v1/invoices", response_model=InvoiceResource)
async def get_latest_invoice():
    return {"id": len(database), **database[-1].model_dump()}


app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", RenameInvoiceCreationDateToCreatedAt),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


def test__rename_invoice_creation_date__when_old_client_uses_the_paths__should_migrate_request_and_response():
    database.clear()
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    create_response = client.post(
        "/v1/invoices", json={"creation_date": "2022-12-01T00:00:00"}
    )
    get_response = client.get("/v1/invoices")

    assert create_response.status_code == 200, create_response.json()
    assert [invoice.model_dump(mode="json") for invoice in database] == [
        {"created_at": "2022-12-01T00:00:00"}
    ]
    assert get_response.status_code == 200, get_response.json()
    assert get_response.json() == {
        "id": 1,
        "creation_date": "2022-12-01T00:00:00",
    }


def test__rename_invoice_creation_date__when_new_client_uses_the_paths__should_use_created_at():
    database.clear()
    client = TestClient(app, headers={"x-api-version": "2023-02-10"})

    client.post("/v1/invoices", json={"created_at": "2022-12-01T00:00:00"})
    response = client.get("/v1/invoices")

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": 1, "created_at": "2022-12-01T00:00:00"}
