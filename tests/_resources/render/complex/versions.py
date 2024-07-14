from datetime import date

from pydantic import Field, conbytes

from cadwyn import Version, VersionBundle
from cadwyn.applications import Cadwyn
from cadwyn.structure import VersionChange
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema

from .classes import A, AlmostEmptyEnum, ModelWithWeirdFields, MyEnum


class MyVersionChange(VersionChange):
    description = ""
    instructions_to_migrate_to_previous_version = (
        enum(MyEnum).didnt_have("baz"),
        enum(AlmostEmptyEnum).didnt_have("foo"),
        schema(A).had(name="NOT_A"),
        schema(ModelWithWeirdFields)
        .field("gaz")
        .existed_as(type=conbytes(strict=True), info=Field(min_length=3, title="Hewwo")),
    )


app = Cadwyn(
    versions=VersionBundle(
        Version(date(2001, 1, 1), MyVersionChange),
        Version(date(2000, 1, 1)),
    )
)
