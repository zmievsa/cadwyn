# ruff: noqa: D205
from cadwyn import (
    RequestInfo,
    VersionChange,
    convert_request_to_next_version_for,
    schema,
)
from users import UserCreateRequest


class MakeUserCountryRequired(VersionChange):
    """'country' is now required when creating a user because the API
    serves users outside the United States and can no longer assume 'USA'.
    """

    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("country").had(default="USA"),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def add_default_value_to_country_field_in_request(request: RequestInfo):
        request.body["country"] = request.body.get("country", "USA")
