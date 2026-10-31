# versions/v2023_02_10.py

from cadwyn import VersionChange, endpoint


class RemoveTaxIdEndpoints(VersionChange):
    """The 'GET /v1/tax_ids' and 'POST /v1/tax_ids' endpoints have been
    removed because tax IDs are now managed on customer records.
    """

    instructions_to_migrate_to_previous_version = (
        endpoint("/v1/tax_ids", ["GET", "POST"]).existed,
    )
