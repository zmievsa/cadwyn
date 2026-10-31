from cadwyn import Cadwyn, Version, VersionBundle, VersionChange, schema
from data.schemas import UserCreateRequest, UserResource


class RenameNameToFullName(VersionChange):
    description = "Renamed `name` to `full_name`"
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("full_name").had(name="name"),
        schema(UserResource).field("full_name").had(name="name"),
    )


app = Cadwyn(
    versions=VersionBundle(
        Version("2025-01-01", RenameNameToFullName),
        Version("2024-05-26"),
    )
)
