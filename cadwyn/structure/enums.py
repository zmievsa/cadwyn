from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .common import _HiddenAttributeMixin


@dataclass(slots=True)
class EnumHadMembersInstruction(_HiddenAttributeMixin):
    enum: type[Enum]
    members: Mapping[str, Any]


@dataclass(slots=True)
class EnumDidntHaveMembersInstruction(_HiddenAttributeMixin):
    enum: type[Enum]
    members: tuple[str, ...]


@dataclass(slots=True)
class EnumInstructionFactory:
    enum_class: type[Enum]

    def had(self, **enum_member_to_value_mapping: Any) -> EnumHadMembersInstruction:
        return EnumHadMembersInstruction(self.enum_class, enum_member_to_value_mapping)

    def didnt_have(self, *enum_members: str) -> EnumDidntHaveMembersInstruction:
        return EnumDidntHaveMembersInstruction(self.enum_class, enum_members)


def enum(enum_class: type[Enum], /) -> EnumInstructionFactory:
    return EnumInstructionFactory(enum_class)


AlterEnumSubInstruction = EnumHadMembersInstruction | EnumDidntHaveMembersInstruction
