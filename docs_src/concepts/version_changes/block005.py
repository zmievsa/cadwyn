from cadwyn import (
    RequestInfo,
    ResponseInfo,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    schema,
)
from invoices import (
    BaseInvoice,
    InvoiceCreateRequest,
    InvoiceResource,
)


class RenameInvoiceCreationDateToCreatedAt(VersionChange):
    """Invoice creation timestamps are now returned as 'created_at' instead of
    'creation_date' to align with the API's other timestamp fields.
    """

    instructions_to_migrate_to_previous_version = (
        schema(BaseInvoice).field("created_at").had(name="creation_date"),
    )

    @convert_request_to_next_version_for(InvoiceCreateRequest)
    def rename_creation_date_to_created_at(request: RequestInfo):
        request.body["created_at"] = request.body.pop("creation_date")

    @convert_response_to_previous_version_for(InvoiceResource)
    def rename_created_at_to_creation_date(response: ResponseInfo):
        response.body["creation_date"] = response.body.pop("created_at")
