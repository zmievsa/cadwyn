from cadwyn import (
    RequestInfo,
    VersionChange,
    convert_request_to_next_version_for,
    schema,
)
from invoices import InvoiceCreateRequest


class RenameInvoiceCreationDateToCreatedAt(VersionChange):
    """Invoice creation timestamps are now returned as 'created_at' instead of
    'creation_date' to align with the API's other timestamp fields.
    """

    instructions_to_migrate_to_previous_version = (
        schema(InvoiceCreateRequest)
        .field("created_at")
        .had(name="creation_date"),
    )

    @convert_request_to_next_version_for(InvoiceCreateRequest)
    def rename_creation_date_to_created_at(request: RequestInfo):
        request.body["created_at"] = request.body.pop("creation_date")
