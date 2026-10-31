from cadwyn import HeadVersion, Version, VersionBundle, VersionChange, schema

from docs_src.concepts.schema_migrations.head.with_internal_notes import User


class HideInternalNotesOfUser(VersionChange):
    description = "Hide the internal notes of 'User' from the API reference"
    instructions_to_migrate_to_previous_version = (
        schema(User).had(description="A user of the API."),
    )


versions = VersionBundle(
    HeadVersion(HideInternalNotesOfUser),
    Version("2001-01-01"),
)
