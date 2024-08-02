import re
from enum import Enum, auto

import pytest

from cadwyn.exceptions import (
    InvalidGenerationInstructionError,
)
from cadwyn.structure import enum
from tests.conftest import CreateRuntimeSchemas, serialize_enum, version_change


class EmptyEnum(Enum):
    pass


class EnumWithOneMember(Enum):
    foo = 83


class EnumWithTwoMembers(Enum):
    foo = 90
    bar = 12


class EnumWithOneMemberAndMethods(Enum):
    foo = 83

    def _hello(self):
        return self.hello_member  # pyright: ignore[reportAttributeAccessIssue]

    @classmethod
    def world(cls) -> int:
        return cls.world_member  # pyright: ignore[reportAttributeAccessIssue]


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
    assert model.hello_member.value == 10  # pyright: ignore[reportAttributeAccessIssue]
    assert model.world_member.value == 20  # pyright: ignore[reportAttributeAccessIssue]
    assert model.foo._hello().value == 10
    assert model.world().value == 20  # pyright: ignore[reportAttributeAccessIssue]


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


def test__enum_had__same_name_as_other_value__error(
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


def test__enum_didnt_have__nonexisting_name__error(
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
