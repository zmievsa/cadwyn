from cadwyn import Version, VersionBundle
from cadwyn.applications import Cadwyn
from cadwyn_core.structure import VersionChange
from cadwyn_core.structure.enums import enum
from cadwyn_core.structure.schemas import schema

from .classes import A, MyEnum


class MyVersionChange(VersionChange):
    description = ""
    instructions_to_migrate_to_previous_version = (
        enum(MyEnum).didnt_have("foo"),
        schema(A).field("foo").didnt_exist,
    )


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", MyVersionChange),
        Version("2000-01-01"),
    )
)
