from cadwyn import (
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    schema,
)

from .users import UserCreateRequest, UserResource


class RenameUserFullNameToName(VersionChange):
    """The 'full_name' of users is now called 'name'."""

    instructions_to_migrate_to_previous_version = (
        schema(UserResource).field("name").had(name="full_name"),
        schema(UserCreateRequest).field("name").had(name="full_name"),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def rename_full_name_to_name(request: RequestInfo):
        request.body["name"] = request.body.pop("full_name")

    @convert_response_to_previous_version_for(UserResource)
    def rename_name_to_full_name(response: ResponseInfo):
        response.body["full_name"] = response.body.pop("name")


versions = VersionBundle(
    Version("2001-01-01", RenameUserFullNameToName),
    Version("2000-01-01"),
)
