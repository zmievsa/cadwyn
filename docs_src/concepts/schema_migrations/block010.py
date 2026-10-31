from cadwyn import VersionChange, schema
from pydantic import BaseModel


class User(BaseModel):
    """A user of the API who can belong to several organizations."""

    name: str


class AllowUsersToBelongToSeveralOrganizations(VersionChange):
    """Users can now belong to several organizations."""

    instructions_to_migrate_to_previous_version = (
        schema(User).had(
            description="A user of the API who belongs to one organization."
        ),
    )
