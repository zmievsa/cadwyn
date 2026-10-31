from cadwyn import Version, VersionBundle, VersionChange, schema
from cadwyn.schema_generation import generate_versioned_models
from pydantic import BaseModel


class MyVersionedSchema(BaseModel):
    full_name: str


class RenameNameToFullName(VersionChange):
    description = "Renamed `name` to `full_name`"
    instructions_to_migrate_to_previous_version = (
        schema(MyVersionedSchema).field("full_name").had(name="name"),
    )


version_bundle = VersionBundle(
    Version("2026-01-01", RenameNameToFullName),
    Version("2025-11-16"),
)

schema_generators = generate_versioned_models(version_bundle)
MyVersionedSchemaFrom2025 = schema_generators["2025-11-16"][MyVersionedSchema]
