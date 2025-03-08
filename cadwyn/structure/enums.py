from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union

from cadwyn._utils import DATACLASS_SLOTS

from .common import _HiddenAttributeMixin


@dataclass(**DATACLASS_SLOTS)
class EnumHadMembersInstruction(_HiddenAttributeMixin):
    enum: type[Enum]
    members: Mapping[str, Any]


@dataclass(**DATACLASS_SLOTS)
class EnumDidntHaveMembersInstruction(_HiddenAttributeMixin):
    enum: type[Enum]
    members: tuple[str, ...]


@dataclass(**DATACLASS_SLOTS)
class EnumInstructionFactory:
    enum_class: type[Enum]

    def had(self, **enum_member_to_value_mapping: Any) -> EnumHadMembersInstruction:
        return EnumHadMembersInstruction(
            is_hidden_from_changelog=False, enum=self.enum_class, members=enum_member_to_value_mapping
        )

    def didnt_have(self, *enum_members: str) -> EnumDidntHaveMembersInstruction:
        return EnumDidntHaveMembersInstruction(
            is_hidden_from_changelog=False, enum=self.enum_class, members=enum_members
        )


def enum(enum_class: type[Enum], /) -> EnumInstructionFactory:
    return EnumInstructionFactory(enum_class)


AlterEnumSubInstruction = Union[EnumHadMembersInstruction, EnumDidntHaveMembersInstruction]
