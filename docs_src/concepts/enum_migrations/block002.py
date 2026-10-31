from enum import Enum

from cadwyn import VersionChange, enum


class MyEnum(str, Enum):
    qux = "qux"


my_enum = MyEnum


class RemoveFooAndBarFromMyEnum(VersionChange):
    """'foo' and 'bar' are no longer returned in 'my_enum' because those
    legacy states are no longer supported.
    """

    instructions_to_migrate_to_previous_version = (
        enum(my_enum).had(foo="baz", bar="bar"),
    )
