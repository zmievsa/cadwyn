from cadwyn import VersionChange, schema
from pydantic import field_validator

from docs_src.concepts.schema_migrations.head.with_foo import MySchema


@field_validator("foo")
def validate_foo(cls, value):
    if ":" not in value:
        raise ValueError("foo must contain a colon")
    return value


class RemoveFooValidatorFromMySchema(VersionChange):
    """'MySchema.foo' no longer requires colon-separated values because the
    field now accepts plain text.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema).validator(validate_foo).existed,
    )
