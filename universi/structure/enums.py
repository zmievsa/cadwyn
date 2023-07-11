from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass(slots=True)
class EnumHadMembersInstruction:
    enum: type[Enum]
    members: Mapping[str, Any]


@dataclass(slots=True)
class EnumDidntHaveMembersInstruction:
    enum: type[Enum]
    members: tuple[str, ...]


@dataclass(slots=True)
class EnumInstructionFactory:
    enum_class: type[Enum]

    def had(self, **enum_member_to_value_mapping) -> EnumHadMembersInstruction:
        return EnumHadMembersInstruction(self.enum_class, enum_member_to_value_mapping)

    def didnt_have(self, *enum_members) -> EnumDidntHaveMembersInstruction:
        return EnumDidntHaveMembersInstruction(self.enum_class, enum_members)


def enum(enum_class: type[Enum], /) -> EnumInstructionFactory:
    return EnumInstructionFactory(enum_class)


AlterEnumSubInstruction = EnumHadMembersInstruction | EnumDidntHaveMembersInstruction
