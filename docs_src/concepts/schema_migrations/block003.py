from cadwyn import VersionChange, schema

from docs_src.concepts.schema_migrations.head.with_described_foo import MySchema


class ChangeMySchemaFooDescription(VersionChange):
    """The documentation for 'MySchema.foo' now explains the field's purpose
    so clients can interpret its value correctly.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(description="Foo"),
    )
