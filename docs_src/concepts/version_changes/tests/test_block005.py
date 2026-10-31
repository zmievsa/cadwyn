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
    from docs_src.concepts.version_changes.block005 import (
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


def test__rename_invoice_creation_date__when_old_client_creates_invoice__should_migrate_request_and_response():
    received_requests.clear()
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.post(
        "/v1/invoices", json={"creation_date": "2022-12-01T00:00:00"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {
        "id": 1,
        "creation_date": "2022-12-01T00:00:00",
    }
    assert [r.model_dump(mode="json") for r in received_requests] == [
        {"created_at": "2022-12-01T00:00:00"}
    ]


def test__rename_invoice_creation_date__when_new_client_creates_invoice__should_use_created_at():
    client = TestClient(app, headers={"x-api-version": "2023-02-10"})

    response = client.post(
        "/v1/invoices", json={"created_at": "2022-12-01T00:00:00"}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"id": 1, "created_at": "2022-12-01T00:00:00"}


@pytest.mark.parametrize(
    "schema_name", ["InvoiceCreateRequest", "InvoiceResource"]
)
def test__rename_invoice_creation_date__in_openapi__should_rename_the_field_in_children_of_base_invoice(
    schema_name: str,
):
    client = TestClient(app)

    old_schemas = client.get("/openapi.json?version=2022-11-16").json()
    new_schemas = client.get("/openapi.json?version=2023-02-10").json()

    old_fields = old_schemas["components"]["schemas"][schema_name]["properties"]
    new_fields = new_schemas["components"]["schemas"][schema_name]["properties"]
    assert "creation_date" in old_fields
    assert "created_at" not in old_fields
    assert "created_at" in new_fields
    assert "creation_date" not in new_fields
