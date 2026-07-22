from pydantic import BaseModel

from cadwyn import (
    ResponseInfo,
    VersionChange,
    convert_response_to_previous_version_for,
    schema,
)


# User from latest version
class User(BaseModel):
    id: str


class ChangeUserIDToString(VersionChange):
    description = (
        "'User.id' is now a string so the API can support identifiers that are "
        "not numeric."
    )
    instructions_to_migrate_to_previous_version = [
        schema(User).field("id").had(type=int),
    ]

    @convert_response_to_previous_version_for(User, check_usage=False)
    def change_id_to_int(response: ResponseInfo): ...
