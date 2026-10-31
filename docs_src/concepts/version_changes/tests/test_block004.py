import sys
from datetime import datetime
from types import ModuleType

import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel


# The page doesn't say where 'created_at' comes from at this point. It is
# declared here and not inherited from 'BaseInvoice' because Cadwyn keeps the
# old field next to the new one when a schema renames a field it inherits.
class InvoiceCreateRequest(BaseModel):
    created_at: datetime


class InvoiceResource(BaseModel):
    id: int
    created_at: datetime


invoices = ModuleType("invoices")
invoices.InvoiceCreateRequest = InvoiceCreateRequest  # ty: ignore[unresolved-attribute]

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "invoices", invoices)
    from docs_src.concepts.version_changes.block004 import (
        RenameInvoiceCreationDateToCreatedAt,
    )

received_requests: list[InvoiceCreateRequest] = []
router = VersionedAPIRouter()


@router.post("/v1/invoices", response_model=InvoiceResource)
async def create_invoice(invoice: InvoiceCreateRequest):
    received_requests.append(invoice)
    return {"id": 1, **invoice.model_dump()}


app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", RenameInvoiceCreationDateToCreatedAt),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


def test__rename_invoice_creation_date__when_old_client_sends_creation_date__should_give_created_at_to_business_logic():
    received_requests.clear()
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.post(
        "/v1/invoices", json={"creation_date": "2022-12-01T00:00:00"}
    )

    assert response.status_code == 200, response.json()
    assert [r.model_dump(mode="json") for r in received_requests] == [
        {"created_at": "2022-12-01T00:00:00"}
    ]


def test__rename_invoice_creation_date__without_response_migration__should_return_created_at_to_old_clients():
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.post(
        "/v1/invoices", json={"creation_date": "2022-12-01T00:00:00"}
    )

    assert response.json() == {"id": 1, "created_at": "2022-12-01T00:00:00"}


def test__rename_invoice_creation_date__in_openapi__should_document_creation_date_only_in_old_version():
    client = TestClient(app)

    old_schemas = client.get("/openapi.json?version=2022-11-16").json()
    new_schemas = client.get("/openapi.json?version=2023-02-10").json()

    old_fields = old_schemas["components"]["schemas"]["InvoiceCreateRequest"]
    new_fields = new_schemas["components"]["schemas"]["InvoiceCreateRequest"]
    assert set(old_fields["properties"]) == {"creation_date"}
    assert set(new_fields["properties"]) == {"created_at"}
