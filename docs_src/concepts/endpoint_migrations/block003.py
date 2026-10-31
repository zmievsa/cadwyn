from cadwyn import VersionChange, endpoint


class AddGetCompanyByIdEndpoint(VersionChange):
    """Clients can now retrieve an individual company with
    'GET /companies/{company_id}' instead of filtering the company list.
    """

    instructions_to_migrate_to_previous_version = (
        endpoint("/companies/{company_id}", ["GET"]).didnt_exist,
    )
