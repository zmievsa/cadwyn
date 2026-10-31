# ruff: noqa: D205
from cadwyn import VersionChange, schema
from pydantic import Field
from users import UserResource


class RemoveZodiacSignFromUser(VersionChange):
    """User responses no longer include 'zodiac_sign' because it can be
    derived from 'date_of_birth' and is rarely used.
    """

    instructions_to_migrate_to_previous_version = (
        schema(UserResource)
        .field("zodiac_sign")
        .existed_as(type=str, info=Field(description="User's magical sign")),
    )
