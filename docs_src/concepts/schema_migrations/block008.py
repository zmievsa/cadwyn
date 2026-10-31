from cadwyn import VersionChange, schema

from docs_src.concepts.schema_migrations.head.with_foo_validator import MySchema


class AddFooValidatorToMySchema(VersionChange):
    """'MySchema.foo' must now contain a colon to distinguish the value's two
    components.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema).validator(MySchema.validate_foo).didnt_exist,
    )
