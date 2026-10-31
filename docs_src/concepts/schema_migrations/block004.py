from cadwyn import VersionChange, schema

from docs_src.concepts.schema_migrations.head.with_described_foo import MySchema


class AddDescriptionToMySchemaFoo(VersionChange):
    """'MySchema.foo' now has a documented meaning so clients do not need to
    infer how to use it.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").didnt_have("description"),
    )
