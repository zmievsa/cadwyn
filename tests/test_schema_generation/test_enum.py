import dataclasses
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import KEEP, Enum, EnumType, Flag, FlagBoundary, IntEnum, IntFlag, StrEnum, auto, member, nonmember
from pathlib import PurePosixPath
from typing import Any, Self
from uuid import UUID

import pytest
from pydantic import TypeAdapter
from typing_extensions import override

from cadwyn.exceptions import (
    InvalidGenerationInstructionError,
    SchemaGenerationError,
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


class EnumWithCustomAutoValues(Enum):
    @staticmethod
    @override
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> int:
        return (count + 1) * 10

    a = auto()
    b = auto()


class StrEnumWithCustomAutoValues(str, Enum):
    @staticmethod
    @override
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> str:
        return name.upper()

    a = auto()
    b = auto()


class EnumBaseWithCustomAutoValues(Enum):
    @staticmethod
    @override
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> str:
        return f"{name}!"


class EnumThatInheritsCustomAutoValues(EnumBaseWithCustomAutoValues):
    a = auto()
    b = auto()


class MixinWithCustomAutoValues:
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> int:
        return (count + 1) * 10


class EnumThatTakesCustomAutoValuesFromMixin(MixinWithCustomAutoValues, Enum):
    _generate_next_value_ = MixinWithCustomAutoValues._generate_next_value_
    a = auto()
    b = auto()


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (EnumWithCustomAutoValues, {"a": 10, "b": 20, "c": 30}),
        (StrEnumWithCustomAutoValues, {"a": "A", "b": "B", "c": "C"}),
        (EnumThatInheritsCustomAutoValues, {"a": "a!", "b": "b!", "c": "c!"}),
        (EnumThatTakesCustomAutoValuesFromMixin, {"a": 10, "b": 20, "c": 30}),
    ],
)
def test__enum_had__auto_value_with_custom_generate_next_value__should_use_the_custom_generate_next_value(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: type[Enum],
    expected: dict[str, Any],
):
    models = create_runtime_schemas(version_change(enum(original).had(c=auto())))

    assert serialize_enum(models["2000-01-01"][original]) == expected
    assert serialize_enum(models["2001-01-01"][original]) == {"a": expected["a"], "b": expected["b"]}


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


class MixinWithBoundaryAttribute:
    _boundary_ = "domain-metadata"


class IntEnumWithBoundaryAttributeFromMixin(MixinWithBoundaryAttribute, IntEnum):
    read = 1
    write = 2


def test__enum_didnt_have__enum_that_inherits_boundary_attribute_from_mixin__should_remove_the_member(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(IntEnumWithBoundaryAttributeFromMixin).didnt_have("write")))

    assert serialize_enum(models["2000-01-01"][IntEnumWithBoundaryAttributeFromMixin]) == {"read": 1}
    assert models["2000-01-01"][IntEnumWithBoundaryAttributeFromMixin]._boundary_ == "domain-metadata"


class FlagWithKeepBoundary(Flag, boundary=KEEP):
    read = 1
    write = 2


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


class EnumWithIntMember(Enum):
    foo = 1


def test__enum_had__value_that_equals_the_current_one_but_differs__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumWithIntMember).had(foo=True)))

    assert models["2000-01-01"][EnumWithIntMember].foo.value is True
    assert models["2001-01-01"][EnumWithIntMember].foo.value == 1
    assert models["2001-01-01"][EnumWithIntMember].foo.value is not True


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


def test__enum_didnt_have__copy_called_with_latest_member__should_return_member_with_same_value(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumWithTwoMembers).didnt_have("bar")))
    old_enum = models["2000-01-01"][EnumWithTwoMembers]

    assert old_enum(EnumWithTwoMembers.foo) is old_enum.foo
    with pytest.raises(ValueError, match="is not a valid EnumWithTwoMembers"):
        old_enum(EnumWithTwoMembers.bar)


class StrEnumWithMissingForMembers(str, Enum):
    a = "a"
    b = "b"

    @classmethod
    @override
    def _missing_(cls, value: object) -> "StrEnumWithMissingForMembers | None":
        return cls.a if isinstance(value, Enum) else None


class PlainEnumWithMissingByName(Enum):
    a = "a"
    b = "b"

    @classmethod
    @override
    def _missing_(cls, value: object) -> "PlainEnumWithMissingByName | None":
        return cls[value.name] if isinstance(value, Enum) else None


def test__enum_didnt_have__str_enum_copy_called_with_latest_member__should_give_missing_the_member(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(StrEnumWithMissingForMembers).didnt_have("b")))
    old_enum = models["2000-01-01"][StrEnumWithMissingForMembers]

    assert old_enum(StrEnumWithMissingForMembers.b) is old_enum.a


def test__enum_had__copy_called_with_latest_member_without_same_value__should_give_missing_the_member(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(PlainEnumWithMissingByName).had(b="old")))
    old_enum = models["2000-01-01"][PlainEnumWithMissingByName]

    assert old_enum(PlainEnumWithMissingByName.b) is old_enum.b
    assert old_enum(PlainEnumWithMissingByName.a) is old_enum.a


def test__enum_didnt_have__conform_flag_copy_called_with_latest_bits_it_doesnt_have__should_raise_value_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class ConformFlag(Flag, boundary=FlagBoundary.CONFORM):
        read = 1
        write = 2

    models = create_runtime_schemas(version_change(enum(ConformFlag).didnt_have("write")))
    old_enum = models["2000-01-01"][ConformFlag]

    assert old_enum(ConformFlag.read) is old_enum.read
    with pytest.raises(ValueError, match="is not a valid ConformFlag"):
        old_enum(ConformFlag.read | ConformFlag.write)


class EnumWithUnhashableValues(Enum):
    a = [1]
    b = [2]


def test__enum_didnt_have__copy_of_enum_with_unhashable_values_called_with_latest_member__should_return_member(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumWithUnhashableValues).didnt_have("b")))
    old_enum = models["2000-01-01"][EnumWithUnhashableValues]

    assert old_enum(EnumWithUnhashableValues.a) is old_enum.a
    with pytest.raises(ValueError, match="is not a valid EnumWithUnhashableValues"):
        old_enum(EnumWithUnhashableValues.b)


def _observe_enum(enum_cls: Any) -> list[Any]:
    return [
        [(member.name, member.value) for member in enum_cls],
        [(name, member.name) for name, member in enum_cls.__members__.items()],
        {member.value: enum_cls(member.value).name for member in enum_cls},
    ]


class EnumWithAlias(Enum):
    a = 1
    b = 1  # noqa: PIE796
    c = 2


class StrEnumWithAlias(str, Enum):
    a = "a"
    b = "a"  # noqa: PIE796
    c = "c"


_ENUM_WITH_ALIAS_NAME = EnumWithAlias.__name__
_STR_ENUM_WITH_ALIAS_NAME = StrEnumWithAlias.__name__


@pytest.mark.parametrize(
    ("instruction", "hand_written"),
    [
        (enum(EnumWithAlias).didnt_have("c"), Enum(_ENUM_WITH_ALIAS_NAME, [("a", 1), ("b", 1)])),
        (enum(EnumWithAlias).didnt_have("a"), Enum(_ENUM_WITH_ALIAS_NAME, [("b", 1), ("c", 2)])),
        (enum(EnumWithAlias).didnt_have("b"), Enum(_ENUM_WITH_ALIAS_NAME, [("a", 1), ("c", 2)])),
        (enum(EnumWithAlias).had(d=2), Enum(_ENUM_WITH_ALIAS_NAME, [("a", 1), ("b", 1), ("c", 2), ("d", 2)])),
        (enum(StrEnumWithAlias).didnt_have("c"), Enum(_STR_ENUM_WITH_ALIAS_NAME, [("a", "a"), ("b", "a")], type=str)),
        (enum(StrEnumWithAlias).didnt_have("a"), Enum(_STR_ENUM_WITH_ALIAS_NAME, [("b", "a"), ("c", "c")], type=str)),
    ],
)
def test__enum_instructions__enum_with_alias__should_behave_like_hand_written_enum(
    create_runtime_schemas: CreateRuntimeSchemas,
    instruction: Any,
    hand_written: type[Enum],
):
    original = instruction.enum
    models = create_runtime_schemas(version_change(instruction))

    assert _observe_enum(models["2000-01-01"][original]) == _observe_enum(hand_written)
    assert _observe_enum(models["2001-01-01"][original]) == _observe_enum(original)


class FlagWithMultiBitAlias(Flag):
    read = 1
    write = 2
    read_write = 3
    execute = 4


def test__enum_didnt_have__flag_with_multi_bit_alias__should_behave_like_hand_written_flag(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(FlagWithMultiBitAlias).didnt_have("execute")))
    old_flag = models["2000-01-01"][FlagWithMultiBitAlias]
    hand_written_definition = [("read", 1), ("write", 2), ("read_write", 3)]

    assert _observe_enum(old_flag) == _observe_enum(Flag(FlagWithMultiBitAlias.__name__, hand_written_definition))
    assert old_flag.read_write is old_flag(3)
    assert old_flag.read_write.value == 3


class EnumWithNonmembers(Enum):
    a = 1
    b = 2
    limit = nonmember(10)
    _limit = nonmember(20)
    __private_limit = 30


def test__enum_didnt_have__enum_with_nonmember_attributes__should_keep_them_as_attributes(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumWithNonmembers).didnt_have("b")))
    old_enum = models["2000-01-01"][EnumWithNonmembers]

    assert list(old_enum.__members__) == ["a"]
    assert old_enum.limit == 10
    assert old_enum._limit == 20
    assert old_enum._EnumWithNonmembers__private_limit == 30  # ty: ignore[unresolved-attribute]


class EnumWithFunctionMember(Enum):
    a = 1
    b = 2

    @member
    def function() -> int:
        return 3


def test__enum_didnt_have__enum_with_function_member__should_keep_the_function_a_member(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumWithFunctionMember).didnt_have("b")))
    old_enum = models["2000-01-01"][EnumWithFunctionMember]

    assert list(old_enum.__members__) == ["a", "function"]
    assert isinstance(old_enum.function, old_enum)
    assert old_enum.function.value() == 3


class EnumWithCustomNewThatTakesMoreThanTheValue(Enum):
    def __new__(cls, value: int, mass: float) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.mass = mass  # ty: ignore[unresolved-attribute]
        return obj

    earth = (1, 5.97)
    mars = (2, 0.642)


class EnumBaseWithCustomNewThatTakesMoreThanTheValue(Enum):
    def __new__(cls, value: int, mass: float) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.mass = mass  # ty: ignore[unresolved-attribute]
        return obj


class EnumThatInheritsCustomNewThatTakesMoreThanTheValue(EnumBaseWithCustomNewThatTakesMoreThanTheValue):
    earth = (1, 5.97)
    mars = (2, 0.642)


@pytest.mark.parametrize(
    "original", [EnumWithCustomNewThatTakesMoreThanTheValue, EnumThatInheritsCustomNewThatTakesMoreThanTheValue]
)
def test__enum_didnt_have__custom_new_that_takes_more_than_the_value__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: type[Enum],
):
    with pytest.raises(
        SchemaGenerationError,
        match=re.escape(
            f'Cadwyn can\'t copy "{original.__name__}" for its versions because its __new__ failed to create the '
            "members from their values: TypeError("
        ),
    ):
        create_runtime_schemas(version_change(enum(original).didnt_have("mars")))


class EnumWithCustomNewThatChangesTheValue(Enum):
    def __new__(cls, value: int) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value * 10
        return obj

    a = 1
    b = 2


class EnumWithCustomNewThatSetsAttributeFromOtherArg(Enum):
    def __new__(cls, value: int, label: str = "default") -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.label = label  # ty: ignore[unresolved-attribute]
        return obj

    a = (1, "first")
    b = (2, "second")


class StrEnumWithCustomNewThatTakesStrFromOtherArg(str, Enum):
    def __new__(cls, value: str, text: str = "default") -> Self:
        obj = str.__new__(cls, text)
        obj._value_ = value
        return obj

    a = ("a", "first")
    b = ("b", "second")


class EnumWithCustomNewThatChangesTheExponentOfTheValue(Enum):
    def __new__(cls, value: str, precision: str = "0.1") -> Self:
        obj = object.__new__(cls)
        obj._value_ = Decimal(value).quantize(Decimal(precision))
        return obj

    a = ("1", "0.01")
    b = ("2", "0.01")


class EnumWithCustomNewThatChangesTheExponentOfAttribute(Enum):
    def __new__(cls, value: int, price: str = "1.0") -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.price = Decimal(price)  # ty: ignore[unresolved-attribute]
        return obj

    a = (1, "1.00")
    b = (2, "1.00")


class EnumWithCustomNewThatChangesTheTypeOfNestedValue(Enum):
    def __new__(cls, value: int, enabled: object = True) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.metadata = {"enabled": enabled}  # ty: ignore[unresolved-attribute]
        return obj

    a = (1, 1)
    b = (2, 1)


@dataclasses.dataclass
class _TagWithLabelThatIsntCompared:
    id: int
    label: str = dataclasses.field(compare=False)


class EnumWithCustomNewThatChangesFieldThatIsntCompared(Enum):
    def __new__(cls, value: int, label: str = "default") -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.tag = _TagWithLabelThatIsntCompared(value, label)  # ty: ignore[unresolved-attribute]
        return obj

    a = (1, "first")
    b = (2, "second")


@pytest.mark.parametrize(
    ("original", "value"),
    [
        (EnumWithCustomNewThatChangesTheExponentOfAttribute, 1),
        (EnumWithCustomNewThatChangesTheTypeOfNestedValue, 1),
        (EnumWithCustomNewThatChangesFieldThatIsntCompared, 1),
        (EnumWithCustomNewThatSetsAttributeFromOtherArg, 1),
        (StrEnumWithCustomNewThatTakesStrFromOtherArg, "a"),
    ],
)
def test__enum_didnt_have__custom_new_that_doesnt_recreate_member_from_value__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: type[Enum],
    value: Any,
):
    with pytest.raises(
        SchemaGenerationError,
        match=re.escape(
            f"Cadwyn can't copy \"{original.__name__}\" for its versions because its __new__ doesn't recreate the "
            f'member "a" from its value {value!r}. Cadwyn only knows the values of the members, not the arguments '
            "that __new__ got. Compute the other attributes of the members from their values instead, e.g. with a "
            "property."
        ),
    ):
        create_runtime_schemas(version_change(enum(original).didnt_have("b")))


_LABELS = {1: "first", 2: "second"}


class EnumWithCustomNewThatRecreatesMembersFromValue(Enum):
    def __new__(cls, value: int, label: "str | None" = None) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.label = label or _LABELS[value]  # ty: ignore[unresolved-attribute]
        return obj

    a = 1
    b = 2


class StrEnumBaseThatAcceptsSeveralValues(str, Enum):
    def __new__(cls, *values: str) -> Self:
        value = "".join(values)
        obj = str.__new__(cls, value)
        obj._value_ = value
        return obj


class StrEnumThatInheritsCustomNewThatRecreatesMembersFromValue(StrEnumBaseThatAcceptsSeveralValues):
    a = "a"
    b = "b"


class StdlibStrEnum(StrEnum):
    a = "a"
    b = "b"


@pytest.mark.parametrize(
    "original",
    [
        EnumWithCustomNewThatRecreatesMembersFromValue,
        StrEnumThatInheritsCustomNewThatRecreatesMembersFromValue,
        StdlibStrEnum,
    ],
)
def test__enum_didnt_have__custom_new_that_recreates_members_from_value__should_behave_like_original(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: Any,
):
    models = create_runtime_schemas(version_change(enum(original).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][original]

    assert _observe_enum(old_enum) == [[("a", original.a.value)], [("a", "a")], {original.a.value: "a"}]
    assert vars(old_enum.a).keys() == vars(original.a).keys()
    assert getattr(old_enum.a, "label", None) == getattr(original.a, "label", None)


def test__enum_had__custom_new__should_pass_the_definition_to_new_like_hand_written_enum(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(
        version_change(enum(EnumWithCustomNewThatRecreatesMembersFromValue).had(c=(3, "third")))
    )
    old_enum: Any = models["2000-01-01"][EnumWithCustomNewThatRecreatesMembersFromValue]

    assert [(m.name, m.value, m.label) for m in old_enum] == [("a", 1, "first"), ("b", 2, "second"), ("c", 3, "third")]


def test__enum_didnt_have__str_mixin_enum__copy_members_should_have_their_own_dict(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(StrEnumWithAlias).didnt_have("c")))

    assert vars(models["2000-01-01"][StrEnumWithAlias].a)["_value_"] == "a"


class StdlibStrEnumWithInitThatSetsNewObject(StrEnum):
    a = "2000-01-01"
    b = "2001-01-01"

    def __init__(self, value: str) -> None:
        self.date = date.fromisoformat(value)


class EnumWithCustomNewAndInitThatSetsNewObject(Enum):
    def __new__(cls, value: str) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    def __init__(self, value: str) -> None:
        self.date = date.fromisoformat(value)

    a = "2000-01-01"
    b = "2001-01-01"


@pytest.mark.parametrize(
    "original", [StdlibStrEnumWithInitThatSetsNewObject, EnumWithCustomNewAndInitThatSetsNewObject]
)
def test__enum_didnt_have__init_that_sets_equal_new_object__should_keep_the_attribute(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: Any,
):
    models = create_runtime_schemas(version_change(enum(original).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][original]

    assert [(m.name, m.value, m.date) for m in old_enum] == [("a", "2000-01-01", date(2000, 1, 1))]


@pytest.mark.parametrize("flag_base", [Flag, IntFlag])
def test__enum_didnt_have__flag_with_custom_new_after_inverting_a_member__should_copy_the_flag(
    create_runtime_schemas: CreateRuntimeSchemas,
    flag_base: type[Flag],
):
    def __new__(cls: Any, value: int) -> Any:  # noqa: N807
        obj = object.__new__(cls) if flag_base is Flag else int.__new__(cls, value)
        obj._value_ = value
        return obj

    original: Any = flag_base("FlagWithCustomNew", {"__new__": __new__, "read": 1, "write": 2, "execute": 4})
    assert ~original.read == original.write | original.execute

    models = create_runtime_schemas(version_change(enum(original).didnt_have("execute")))
    old_flag: Any = models["2000-01-01"][original]

    assert serialize_enum(old_flag) == {"read": 1, "write": 2}
    assert ~old_flag.read is old_flag.write


class EnumWithAliasAndInitThatSetsTheName(Enum):
    def __new__(cls, value: int) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    def __init__(self, value: int) -> None:
        self.label = self.name

    a = 1
    b = 1  # noqa: PIE796
    c = 2


def test__enum_didnt_have__canonical_member_of_alias_with_custom_new__should_behave_like_hand_written_enum(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumWithAliasAndInitThatSetsTheName).didnt_have("a")))
    old_enum: Any = models["2000-01-01"][EnumWithAliasAndInitThatSetsTheName]

    assert [(name, m.name, m.value, m.label) for name, m in old_enum.__members__.items()] == [
        ("b", "b", 1, "b"),
        ("c", "c", 2, "c"),
    ]


class EnumBaseWithSlotThatCustomNewSets(Enum):
    __slots__ = ("mass",)

    def __new__(cls, value: int, mass: float = 0) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.mass = mass
        return obj


class EnumThatInheritsSlotThatCustomNewSets(EnumBaseWithSlotThatCustomNewSets):
    earth = (1, 5.97)
    mars = (2, 0.642)


class EnumWithSlotThatCustomNewSets(Enum):
    __slots__ = ("mass",)

    def __new__(cls, value: int, mass: float = 0) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.mass = mass
        return obj

    earth = (1, 5.97)
    mars = (2, 0.642)


class EnumBaseWithIteratorSlotThatCustomNewSets(Enum):
    __slots__ = iter(["mass"])

    def __new__(cls, value: int, mass: float = 0) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.mass = mass  # ty: ignore[unresolved-attribute]
        return obj


class EnumThatInheritsIteratorSlotThatCustomNewSets(EnumBaseWithIteratorSlotThatCustomNewSets):
    earth = (1, 5.97)
    mars = (2, 0.642)


class EnumBaseWithSlotThatSubclassShadows(Enum):
    __slots__ = ("mass",)


class EnumWithSlotThatShadowsSlotThatCustomNewSets(EnumBaseWithSlotThatSubclassShadows):
    __slots__ = ("mass",)

    def __new__(cls, value: int, mass: float = 0) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        EnumBaseWithSlotThatSubclassShadows.mass.__set__(obj, mass)
        obj.mass = value * 2
        return obj

    earth = (1, 5.97)
    mars = (2, 0.642)


@pytest.mark.parametrize(
    "original",
    [
        EnumThatInheritsSlotThatCustomNewSets,
        EnumWithSlotThatCustomNewSets,
        EnumThatInheritsIteratorSlotThatCustomNewSets,
        EnumWithSlotThatShadowsSlotThatCustomNewSets,
    ],
)
def test__enum_didnt_have__custom_new_that_sets_slot_from_other_arg__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: type[Enum],
):
    with pytest.raises(
        SchemaGenerationError,
        match=re.escape(
            f"Cadwyn can't copy \"{original.__name__}\" for its versions because its __new__ doesn't recreate the "
            'member "earth" from its value 1.'
        ),
    ):
        create_runtime_schemas(version_change(enum(original).didnt_have("mars")))


class EnumWithSlotThatInitSets(Enum):
    __slots__ = ("double",)

    def __init__(self, value: int) -> None:
        self.double = value * 2

    a = 1
    b = 2


class EnumWithIteratorSlotThatInitSets(Enum):
    __slots__ = iter(["double"])

    def __init__(self, value: int) -> None:
        self.double = value * 2

    a = 1
    b = 2


@pytest.mark.parametrize("original", [EnumWithSlotThatInitSets, EnumWithIteratorSlotThatInitSets])
def test__enum_didnt_have__enum_with_own_slots__should_copy_the_slots(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: type[Enum],
):
    models = create_runtime_schemas(version_change(enum(original).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][original]

    assert [(m.name, m.double) for m in old_enum] == [("a", 2)]
    assert "double" not in vars(old_enum.a)

    assert [(m.name, m.double) for m in old_enum] == [("a", 2)]


class EnumBaseWithSetAndUnsetSlots(Enum):
    __slots__ = ("__cache", "double")

    def __new__(cls, value: int) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        obj.double = value * 2
        return obj


class EnumThatInheritsSetAndUnsetSlots(EnumBaseWithSetAndUnsetSlots):
    a = 1
    b = 2


def test__enum_didnt_have__custom_new_that_sets_slot_from_value__should_copy_the_enum(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(EnumThatInheritsSetAndUnsetSlots).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][EnumThatInheritsSetAndUnsetSlots]

    assert [(m.name, m.double) for m in old_enum] == [("a", 2)]


class _Tag:
    pass


class EnumWithCustomNewAndInitThatSetsNewObjectOfOtherClass(Enum):
    def __new__(cls, value: int) -> Self:
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    def __init__(self, value: int) -> None:
        self.tag = _Tag()

    a = 1
    b = 2


def test__enum_didnt_have__custom_new_that_sets_new_object_of_other_class__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        SchemaGenerationError,
        match=re.escape(
            'Cadwyn can\'t copy "EnumWithCustomNewAndInitThatSetsNewObjectOfOtherClass" for its versions because its '
            '__new__ doesn\'t recreate the member "a" from its value 1.'
        ),
    ):
        create_runtime_schemas(
            version_change(enum(EnumWithCustomNewAndInitThatSetsNewObjectOfOtherClass).didnt_have("b"))
        )


@pytest.mark.parametrize(
    "make_attribute",
    [
        lambda: datetime(2000, 1, 1, 12, tzinfo=timezone(timedelta(hours=1), "CET")),
        lambda: time(12, 30, tzinfo=timezone.utc),
        lambda: timedelta(days=1),
        lambda: UUID("12345678-1234-5678-1234-567812345678"),
        lambda: PurePosixPath("a/b"),
    ],
)
def test__enum_didnt_have__custom_new_with_init_that_sets_equal_new_value_object__should_keep_the_attribute(
    create_runtime_schemas: CreateRuntimeSchemas,
    make_attribute: Callable[[], object],
):
    def __new__(cls: Any, value: int) -> Any:  # noqa: N807
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    def __init__(self: Any, value: int) -> None:  # noqa: N807
        self.attribute = make_attribute()

    original: Any = Enum("EnumWithInitThatSetsValueObject", {"__new__": __new__, "__init__": __init__, "a": 1, "b": 2})
    models = create_runtime_schemas(version_change(enum(original).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][original]

    assert [(m.name, m.attribute) for m in old_enum] == [("a", make_attribute())]


@pytest.mark.parametrize(
    ("original", "expected_value", "expected_json"),
    [
        (EnumWithCustomNewThatChangesTheExponentOfTheValue, Decimal("1.00"), b'"1.00"'),
        (EnumWithCustomNewThatChangesTheValue, 10, b"10"),
    ],
)
def test__enum_didnt_have__custom_new_whose_members_have_only_their_values__should_keep_the_exact_values(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: Any,
    expected_value: Any,
    expected_json: bytes,
):
    models = create_runtime_schemas(version_change(enum(original).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][original]

    assert [(m.name, repr(m.value)) for m in old_enum] == [("a", repr(expected_value))]
    assert TypeAdapter(old_enum).dump_json(old_enum.a) == expected_json
    assert TypeAdapter(old_enum).validate_python(original.a) is old_enum.a


class TupleEnumWithCustomNew(tuple, Enum):  # noqa: SLOT001
    def __new__(cls, *coordinates: int) -> Self:
        obj = tuple.__new__(cls, coordinates)
        obj._value_ = coordinates
        return obj

    a = (1, 2)
    b = (3, 4)


def test__enum_didnt_have__tuple_enum_with_custom_new__should_keep_the_exact_values(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(TupleEnumWithCustomNew).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][TupleEnumWithCustomNew]

    original = TupleEnumWithCustomNew.a
    assert [(m.name, m.value, tuple(m)) for m in old_enum] == [("a", original.value, tuple(original))]


@pytest.mark.parametrize(
    "original", [EnumWithTwoMembers, StrEnumWithAlias, EnumWithCustomNewThatRecreatesMembersFromValue]
)
def test__enum_didnt_have__member_attributes_set_after_creation__should_keep_the_attributes(
    create_runtime_schemas: CreateRuntimeSchemas,
    original: type[Enum],
):
    retained = next(iter(original))
    retained.display_label = "A"  # ty: ignore[unresolved-attribute]
    try:
        models = create_runtime_schemas(version_change(enum(original).didnt_have(list(original.__members__)[-1])))
        for version in ("2001-01-01", "2000-01-01"):
            copied = models[version][original].__members__[retained.name]
            assert copied.display_label == "A"  # ty: ignore[unresolved-attribute]
            assert copied.value is retained.value
            assert copied is not retained
    finally:
        del retained.display_label  # ty: ignore[unresolved-attribute]


class LabelMeta(EnumType):
    @override
    def __new__(mcls, cls: str, bases: tuple[type, ...], classdict: Any, **kwargs: Any) -> Any:
        result: Any = super().__new__(mcls, cls, bases, classdict, **kwargs)
        for value in result:
            value.label = value.name.upper()
        return result


class LabeledEnum(Enum, metaclass=LabelMeta):
    a = 1
    b = 2


def test__enum_didnt_have__member_attributes_set_by_metaclass__should_keep_the_attributes(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    models = create_runtime_schemas(version_change(enum(LabeledEnum).didnt_have("b")))
    old_enum: Any = models["2000-01-01"][LabeledEnum]

    assert old_enum.a.label == LabeledEnum.a.label == "A"  # ty: ignore[unresolved-attribute]


def test__enum_didnt_have__member_slots_set_after_creation__should_keep_own_and_shadowed_slots(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Base(Enum):
        __slots__ = ("label",)

        def __new__(cls, value: int) -> Self:
            obj = object.__new__(cls)
            obj._value_ = value
            return obj

    class Kind(Base):
        __slots__ = ("label", "unused")
        a = 1
        b = 2

    Base.label.__set__(Kind.a, "base label")
    Kind.a.label = "A"
    models = create_runtime_schemas(version_change(enum(Kind).didnt_have("b")))
    old = models["2000-01-01"][Kind]

    assert old.a.label == "A"
    assert Base.label.__get__(old.a) == "base label"
    assert not hasattr(old.a, "unused")


def test__enum_had__replaced_member__should_not_keep_its_old_attributes(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Kind(Enum):
        a = 1
        b = 2

    Kind.a.label = "A"  # ty: ignore[unresolved-attribute]
    models = create_runtime_schemas(version_change(enum(Kind).had(a=3)))
    old = models["2000-01-01"][Kind]

    assert old.a.value == 3
    assert not hasattr(old.a, "label")


@pytest.mark.parametrize("use_slots", [False, True])
def test__enum_didnt_have__constructor_sets_member_order__should_keep_the_reconstructed_order(
    create_runtime_schemas: CreateRuntimeSchemas,
    use_slots: bool,
):
    class Ordered(Enum):
        __slots__ = ("ordinal",) if use_slots else ()

        def __new__(cls, value: int) -> Self:
            obj = object.__new__(cls)
            obj._value_ = value
            obj.ordinal = len(cls.__members__)  # ty: ignore[unresolved-attribute]
            return obj

        a = 1
        b = 2
        c = 3

    models = create_runtime_schemas(version_change(enum(Ordered).didnt_have("a")))
    old: Any = models["2000-01-01"][Ordered]

    assert [(m.name, m.ordinal) for m in old] == [("b", 0), ("c", 1)]
    assert [(m.name, m.ordinal) for m in Ordered] == [("a", 0), ("b", 1), ("c", 2)]  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize("use_slots", [False, True])
def test__enum_didnt_have__constructor_omits_state_for_first_member__should_not_restore_constructor_state(
    create_runtime_schemas: CreateRuntimeSchemas,
    use_slots: bool,
):
    class Ordered(Enum):
        __slots__ = ("previous",) if use_slots else ()

        def __new__(cls, value: int) -> Self:
            obj = object.__new__(cls)
            obj._value_ = value
            if cls.__members__:
                obj.previous = list(cls.__members__)[-1]  # ty: ignore[unresolved-attribute]
            return obj

        a = 1
        b = 2
        c = 3

    Ordered.b.display_label = "B"  # ty: ignore[unresolved-attribute]
    models = create_runtime_schemas(version_change(enum(Ordered).didnt_have("a")))
    old: Any = models["2000-01-01"][Ordered]

    assert not hasattr(old.b, "previous")
    assert old.c.previous == "b"
    assert old.b.display_label == "B"


def test__enum_didnt_have__metaclass_requires_class_arguments__should_keep_existing_members_labels(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Meta(EnumType):
        @override
        def __new__(  # ty: ignore[invalid-method-override] # Python accepts required metaclass keywords.
            mcls, cls: str, bases: tuple[type, ...], classdict: Any, *, labels: dict[str, str], **kwargs: Any
        ) -> Any:
            result: Any = super().__new__(mcls, cls, bases, classdict, **kwargs)
            for value in result:
                value.label = labels[value.name]
            return result

    class Kind(Enum, metaclass=Meta, labels={"a": "A", "b": "B"}):
        a = 1
        b = 2

    models = create_runtime_schemas(version_change(enum(Kind).didnt_have("b")))
    old: Any = models["2000-01-01"][Kind]

    assert old.a.label == "A"
    assert old.a.value == 1
