from enum import Enum

from cadwyn import VersionChange, enum


class MyEnum(str, Enum):
    baz = "baz"
    foo = "foo"
    bar = "bar"


class AddFooAndBarToMyEnum(VersionChange):
    """'MyEnum' now includes 'foo' and 'bar' so clients can represent the
    newly supported states.
    """

    instructions_to_migrate_to_previous_version = (
        enum(MyEnum).didnt_have("foo", "bar"),
    )
