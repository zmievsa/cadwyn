# ruff: noqa: D205, E402
# The docs show each version change with its own imports
from cadwyn import VersionChange, schema
from users import BaseUser


class RemoveUserMiddleNameFromLatestVersion(VersionChange):
    """User requests and responses no longer include 'middle_name' because
    the product no longer uses it.
    """

    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("middle_name").didnt_exist,
    )


from cadwyn import VersionChange, schema
from pydantic import Field
from users import BaseUser


class RemoveMiddleNameFromUser(VersionChange):
    """User requests and responses no longer include 'middle_name' because
    the product no longer uses it.
    """

    instructions_to_migrate_to_previous_version = (
        schema(BaseUser)
        .field("middle_name")
        .existed_as(
            type=str | None,
            info=Field(description="User's Middle Name", default=None),
        ),
    )
