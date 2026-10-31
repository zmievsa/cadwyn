# ruff: noqa: D205
from cadwyn import (
    ResponseInfo,
    VersionChange,
    convert_response_to_previous_version_for,
    enum,
)
from users import UserResource, UserRoleEnum


class AddModeratorRoleToUser(VersionChange):
    """Users can now have a 'moderator' role with administrative access but
    without permission to create or remove admins, enabling safer delegation.
    """

    instructions_to_migrate_to_previous_version = (
        enum(UserRoleEnum).didnt_have("moderator"),
    )

    @convert_response_to_previous_version_for(UserResource)
    def change_moderator_to_regular(response: ResponseInfo):
        if response.body["role"] == "moderator":
            response.body["role"] = "regular"
