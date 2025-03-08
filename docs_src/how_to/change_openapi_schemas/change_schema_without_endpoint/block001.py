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
        "Change users' ID field to a string to support any kind of ID. "
        "Be careful: if you use a non-integer ID in a new version and "
        "try to get it from the old version, the ID will be zero in response"
    )
    instructions_to_migrate_to_previous_version = [
        schema(User).field("id").had(type=int),
    ]

    @convert_response_to_previous_version_for(User)
    def change_id_to_int(response: ResponseInfo): ...
