import re
import sys
from enum import Enum, Flag, IntFlag, auto
from typing import Any

import pytest

from cadwyn.exceptions import (
    InvalidGenerationInstructionError,
)
from cadwyn.structure import enum
from tests.conftest import CreateRuntimeSchemas, serialize_enum, version_change


class EmptyEnum(Enum):
    """My docstring"""


class EnumWithOneMember(Enum):
    foo = 83


class EnumWithTwoMembers(Enum):
    foo = 90
    bar = 12


class EnumWithOneMemberAndMethods(Enum):
    foo = 83

    def _hello(self) -> "EnumWithOneMemberAndMethods":
        return self.__class__.__members__["hello_member"]

    @classmethod
    def world(cls) -> "EnumWithOneMemberAndMethods":
        return cls.__members__["world_member"]


def test__enum_had__should_copy_docstring_too(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(version_change(enum(EmptyEnum).had(b=auto())))
    assert models["2000-01-01"][EmptyEnum].__doc__ == "My docstring"
    assert models["2001-01-01"][EmptyEnum].__doc__ == "My docstring"


def test__enum_had__original_enum_is_empty(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(version_change(enum(EmptyEnum).had(b=auto())))
    assert serialize_enum(models["2000-01-01"][EmptyEnum]) == {"b": 1}


def test__enum_had__original_enum_has_methods__all_methods_are_preserved(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(
        version_change(enum(EnumWithOneMemberAndMethods).had(hello_member=10, world_member=20))
    )
    model = models["2000-01-01"][EnumWithOneMemberAndMethods]
    assert model.foo.value == 83
    assert model.hello_member.value == 10  # ty: ignore[unresolved-attribute]
    assert model.world_member.value == 20  # ty: ignore[unresolved-attribute]
    assert model.foo._hello().value == 10
    assert model.world().value == 20


def test__enum_had__original_enum_is_nonempty(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(version_change(enum(EnumWithOneMember).had(b=7)))
    assert serialize_enum(models["2000-01-01"][EnumWithOneMember]) == {"foo": 83, "b": 7}


def test__enum_didnt_have__original_enum_has_one_member(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(version_change(enum(EnumWithOneMember).didnt_have("foo")))
    assert serialize_enum(models["2000-01-01"][EnumWithOneMember]) == {}


def test__enum_didnt_have__original_enum_has_two_members(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(version_change(enum(EnumWithTwoMembers).didnt_have("foo")))
    assert serialize_enum(models["2000-01-01"][EnumWithTwoMembers]) == {"bar": 12}


def test__enum_had__original_schema_is_empty(create_runtime_schemas: CreateRuntimeSchemas):
    models = create_runtime_schemas(version_change(enum(EmptyEnum).had(b=7)))
    assert serialize_enum(models["2000-01-01"][EmptyEnum]) == {"b": 7}


class FlagWithTwoMembers(Flag):
    read = 1
    write = 2


class IntFlagWithTwoMembers(IntFlag):
    read = 1
    write = 2


def _observe_flag_enum(flag_enum: Any) -> list[Any]:
    read_and_execute = flag_enum.read | flag_enum.execute
    return [
        serialize_enum(flag_enum),
        repr(read_and_execute),
        read_and_execute.value,
        type(read_and_execute) is flag_enum,
        flag_enum(5) is read_and_execute,
        flag_enum.read in read_and_execute,
        repr(read_and_execute & flag_enum.read),
        repr(~flag_enum.read),
    ]


@pytest.mark.parametrize("flag_base", [Flag, IntFlag])
def test__enum_didnt_have_and_had__flag_enum__should_behave_like_hand_written_flag_enum(
    create_runtime_schemas: CreateRuntimeSchemas,
    flag_base: type[Flag],
):
    original = FlagWithTwoMembers if flag_base is Flag else IntFlagWithTwoMembers
    models = create_runtime_schemas(version_change(enum(original).didnt_have("write"), enum(original).had(execute=4)))
    hand_written = flag_base(original.__name__, {"read": 1, "execute": 4})

    assert _observe_flag_enum(models["2000-01-01"][original]) == _observe_flag_enum(hand_written)
    assert serialize_enum(models["2001-01-01"][original]) == {"read": 1, "write": 2}


if sys.version_info >= (3, 11):
    from enum import KEEP

    class FlagWithKeepBoundary(Flag, boundary=KEEP):
        read = 1
        write = 2


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Flag boundaries were added in Python 3.11")
def test__enum_didnt_have__flag_enum_with_custom_boundary__should_keep_the_boundary(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(FlagWithKeepBoundary).didnt_have("write")))
    # With the default boundary of Flag, 8 would raise a ValueError because it isn't a combination of members
    assert models["2000-01-01"][FlagWithKeepBoundary](8).value == 8


def test__enum_had__same_name_as_other_value__should_raise_invalid_generation_instruction_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a member "foo" to "EnumWithOneMember" in '
            '"MyVersionChange" but there is already a member with that name and value.',
        ),
    ):
        create_runtime_schemas(version_change(enum(EnumWithOneMember).had(foo=83)))


def test__enum_didnt_have__nonexisting_name__should_raise_invalid_generation_instruction_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a member "hoo" from "EnumWithOneMember" in '
            '"MyVersionChange" but it doesn\'t have such a member.',
        ),
    ):
        create_runtime_schemas(version_change(enum(EnumWithOneMember).didnt_have("hoo")))
