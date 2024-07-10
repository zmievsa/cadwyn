from datetime import date

from cadwyn import Version, VersionBundle
from cadwyn.applications import Cadwyn
from cadwyn.structure import VersionChange
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema

from .classes import A, MyEnum


class MyVersionChange(VersionChange):
    description = ""
    instructions_to_migrate_to_previous_version = (
        enum(MyEnum).didnt_have("foo"),
        schema(A).field("foo").didnt_exist,
    )


app = Cadwyn(
    versions=VersionBundle(
        Version(date(2001, 1, 1), MyVersionChange),
        Version(date(2000, 1, 1)),
    )
)
