from cadwyn import VersionChange, schema

from docs_src.concepts.schema_migrations.head.with_foo import MySchema


class RenameOtherSchemaToMySchema(VersionChange):
    """'OtherSchema' has been renamed to 'MySchema' to match the resource name
    used throughout the API.
    """

    instructions_to_migrate_to_previous_version = (
        schema(MySchema).had(name="OtherSchema"),
    )
