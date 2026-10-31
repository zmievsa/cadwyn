from cadwyn import VersionChange, schema
from pydantic import Field

from docs_src.concepts.schema_migrations.head.without_foo import MySchema


class RemoveFooFromMySchema(VersionChange):
    """'MySchema.foo' has been removed because clients no longer need the
    legacy list of values.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema)
        .field("foo")
        .existed_as(type=list[str], info=Field(description="Foo")),
    )
