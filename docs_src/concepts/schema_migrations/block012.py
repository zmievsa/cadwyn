from cadwyn import VersionChange, schema
from pydantic import BaseModel, field_validator


def must_be_positive(value: int) -> int:
    if value <= 0:
        raise ValueError("must be positive")
    return value


class Dimensions(BaseModel):
    width: int
    height: int

    check_width = field_validator("width")(must_be_positive)
    check_height = field_validator("height")(must_be_positive)


class RequirePositiveWidth(VersionChange):
    """Width must now be positive, as height already was."""

    instructions_to_migrate_to_previous_version = (
        schema(Dimensions).validator("check_width").didnt_exist,
    )
