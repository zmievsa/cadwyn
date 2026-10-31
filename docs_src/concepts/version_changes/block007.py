from cadwyn import (
    ResponseInfo,
    VersionChange,
    convert_response_to_previous_version_for,
)


class ReplaceInvoiceNotFoundStatusCode400With404(VersionChange):
    """Missing invoices from 'GET /v1/invoices' now return 404 instead of 400
    so clients can distinguish absent resources from invalid requests.
    """

    instructions_to_migrate_to_previous_version = ()

    @convert_response_to_previous_version_for(
        "/v1/invoices", ["GET"], migrate_http_errors=True
    )
    def replace_404_with_400(response: ResponseInfo):
        if response.status_code == 404:
            response.status_code = 400
