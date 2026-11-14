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
from pydantic import BaseModel


class User(BaseModel):
    full_name: str


class RenameNameToFullName(VersionChange):
    description = "'name' is now called 'full_name'"
    instructions_to_migrate_to_previous_version = (
        schema(User).field("full_name").had(name="name"),
    )

    @convert_request_to_next_version_for(User)
    def rename_name_to_full_name(request: RequestInfo):
        request.body["full_name"] = request.body.pop("name")

    @convert_response_to_previous_version_for(User)
    def rename_full_name_to_name(response: ResponseInfo):
        response.body["name"] = response.body.pop("full_name")


versions = VersionBundle(
    Version("2001-01-01", RenameNameToFullName),
    Version("2000-01-01"),
)
