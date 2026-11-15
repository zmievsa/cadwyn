from pydantic import BaseModel

from cadwyn_core import (
    RequestInfo,
    Version,
    VersionBundle,
    VersionChange,
    convert_request_to_next_version_for,
    migrate_request,
    schema,
)


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


versions = VersionBundle(
    Version("2001-01-01", RenameNameToFullName),
    Version("2000-01-01"),
)

user = migrate_request(
    versions, User, body={"name": "John"}, version="2000-01-01"
).body
