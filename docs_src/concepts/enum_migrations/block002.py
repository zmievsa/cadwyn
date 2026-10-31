from enum import Enum

from cadwyn import VersionChange, enum


class MyEnum(str, Enum):
    qux = "qux"


class RemoveFooAndBarFromMyEnum(VersionChange):
    """'foo' and 'bar' are no longer returned in 'MyEnum' because those
    legacy states are no longer supported.
    """

    instructions_to_migrate_to_previous_version = (
        enum(MyEnum).had(foo="baz", bar="bar"),
    )
