# ruff: noqa: D205
from cadwyn import VersionChange, schema
from users import UserCreateRequest


class MakeUserPhoneNonNullableInLatest(VersionChange):
    """New user requests now require a phone number so accounts can support
    SMS verification and two-factor authentication.
    """

    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("phone").had(type=str),
        schema(UserCreateRequest).field("phone").didnt_have("default"),
    )


class AddRequiredPhoneToUser(VersionChange):
    """New user requests now require a phone number so accounts can support
    SMS verification and two-factor authentication.
    """

    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest)
        .field("phone")
        .had(type=str | None, default=None),
    )
