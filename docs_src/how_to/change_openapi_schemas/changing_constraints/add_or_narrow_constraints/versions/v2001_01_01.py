# ruff: noqa: D205
from cadwyn import VersionChange, schema
from users import UserCreateRequest


class LimitUserNamesTo250CharactersInLatest(VersionChange):
    """User names in creation requests are now limited to 250 characters
    to prevent impractically large values.
    """

    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("name").had(max_length=250),
    )


class LimitUserNamesTo250Characters(VersionChange):
    """User names in creation requests are now limited to 250 characters
    to prevent impractically large values.
    """

    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("name").didnt_have("max_length"),
    )
