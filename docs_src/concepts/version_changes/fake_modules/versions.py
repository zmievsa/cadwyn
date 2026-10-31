from cadwyn import (
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    convert_response_to_previous_version_for,
    schema,
)

from .users import UserResource


class RenameUserFullNameToName(VersionChange):
    """'UserResource.full_name' is now called 'name'."""

    instructions_to_migrate_to_previous_version = (
        schema(UserResource).field("name").had(name="full_name"),
    )

    @convert_response_to_previous_version_for(UserResource)
    def rename_name_to_full_name(response: ResponseInfo):
        response.body["full_name"] = response.body.pop("name")


versions = VersionBundle(
    Version("2001-01-01", RenameUserFullNameToName),
    Version("2000-01-01"),
)
