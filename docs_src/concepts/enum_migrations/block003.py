from enum import Enum

from cadwyn import VersionChange, enum

_LABELS = {"red": "Red", "green": "Green", "blue": "Blue"}


class Color(str, Enum):
    red = "red"
    green = "green"
    blue = "blue"

    @property
    def label(self) -> str:
        return _LABELS[self.value]


class RemoveBlueFromColor(VersionChange):
    """'blue' is a new color."""

    instructions_to_migrate_to_previous_version = (
        enum(Color).didnt_have("blue"),
    )
