from cadwyn import VersionChange, schema

from docs_src.concepts.schema_migrations.head.with_foo import MySchema


class AddFooToMySchema(VersionChange):
    """'MySchema.foo' is now available so clients can read the value directly
    instead of deriving it from other fields.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").didnt_exist,
    )
