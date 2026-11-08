from typing import Generic, TypeVar

from cadwyn import Cadwyn, Version, VersionBundle, VersionChange, schema
from pydantic import BaseModel


class Item(BaseModel):
    full_name: str


T = TypeVar("T", bound=Item)


class Page(BaseModel, Generic[T]):
    items: list[T]


class RenameNameToFullName(VersionChange):
    description = "Renamed `name` to `full_name`"
    instructions_to_migrate_to_previous_version = (
        schema(Item).field("full_name").had(name="name"),
    )


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", RenameNameToFullName),
        Version("2000-01-01"),
    )
)
