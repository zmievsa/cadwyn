# ruff: noqa: ANN001, D205
import datetime

from cadwyn import VersionChange, schema
from pydantic import field_validator
from users import BaseUser


@field_validator("date_of_birth", mode="before")
def convert_date_of_birth_to_date(cls, v: datetime.date | datetime.datetime):
    if isinstance(v, datetime.datetime):
        return v.date()
    return v


class ChangeUserDateOfBirthFromDatetimeToDateInLatest(VersionChange):
    """'User.date_of_birth' is now a calendar date instead of a timestamp
    because a time of day is not meaningful for a birth date.
    """

    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("date_of_birth").had(type=datetime.date),
        # This step is only necessary in Pydantic 2 because datetime
        # won't be converted to date automatically.
        schema(BaseUser).validator(convert_date_of_birth_to_date).existed,
    )


class ChangeUserDateOfBirthFromDatetimeToDate(VersionChange):
    """'User.date_of_birth' is now a calendar date instead of a timestamp
    because a time of day is not meaningful for a birth date.
    """

    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("date_of_birth").had(type=datetime.datetime),
        schema(BaseUser).validator(convert_date_of_birth_to_date).didnt_exist,
    )
