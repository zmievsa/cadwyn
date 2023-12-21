import inspect
import re
from enum import auto
from typing import Any

import pytest

from cadwyn.exceptions import (
    InvalidGenerationInstructionError,
)
from cadwyn.structure import (
    enum,
)
from tests.conftest import (
    CreateLocalSimpleVersionedPackages,
    LatestModuleFor,
    _FakeModuleWithEmptyClasses,
    serialize,
)


@pytest.fixture()
def latest(latest_module_for: LatestModuleFor):
    return latest_module_for(
        """
    from enum import Enum

    class EnumWithOneMember(Enum):
        foo = 83

    class EnumWithTwoMembers(Enum):
        foo = 90
        bar = 12
    """,
    )


def test__enum_had__original_enum_is_empty(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_empty_classes: Any,
):
    v1 = create_local_simple_versioned_packages(enum(latest_with_empty_classes.EmptyEnum).had(b=auto()))

    assert serialize(v1.EmptyEnum) == {"b": 1}


def test__enum_had__original_enum_has_methods__all_methods_are_preserved(
    latest_module_for: LatestModuleFor,
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    latest = latest_module_for(
        """
    from enum import Enum

    class EnumWithOneMemberAndMethods(Enum):
        foo = 83

        def _hello(self):
            pass

        def world(self, num: str) -> int:
            return int(num)

    """,
    )
    v1 = create_local_simple_versioned_packages(
        enum(latest.EnumWithOneMemberAndMethods).had(b=7),
    )

    assert inspect.getsource(v1.EnumWithOneMemberAndMethods) == (
        "class EnumWithOneMemberAndMethods(Enum):\n"
        "    foo = 83\n"
        "    b = 7\n\n"
        "    def _hello(self):\n"
        "        pass\n\n"
        "    def world(self, num: str) -> int:\n"
        "        return int(num)\n"
    )


def test__enum_had__original_enum_is_nonempty(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest: Any,
):
    v1 = create_local_simple_versioned_packages(
        enum(latest.EnumWithOneMember).had(b=7),
    )

    assert serialize(v1.EnumWithOneMember) == {"foo": 83, "b": 7}


def test__enum_didnt_have__original_enum_has_one_member(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest: Any,
):
    v1 = create_local_simple_versioned_packages(
        enum(latest.EnumWithOneMember).didnt_have("foo"),
    )

    assert serialize(v1.EnumWithOneMember) == {}


def test__enum_didnt_have__original_enum_has_two_members(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest: Any,
):
    v1 = create_local_simple_versioned_packages(enum(latest.EnumWithTwoMembers).didnt_have("foo"))
    assert serialize(v1.EnumWithTwoMembers) == {"bar": 12}


def test__enum_had__original_schema_is_empty(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    v1 = create_local_simple_versioned_packages(
        enum(latest_with_empty_classes.EmptyEnum).had(b=7),
    )

    assert serialize(v1.EmptyEnum) == {"b": 7}


def test__enum_had__same_name_as_other_value__error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest: Any,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a member "foo" to "EnumWithOneMember" in '
            '"MyVersionChange" but there is already a member with that name and value.',
        ),
    ):
        create_local_simple_versioned_packages(enum(latest.EnumWithOneMember).had(foo=83))


def test__enum_didnt_have__nonexisting_name__error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest: Any,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a member "hoo" from "EnumWithOneMember" in '
            '"MyVersionChange" but it doesn\'t have such a member.',
        ),
    ):
        create_local_simple_versioned_packages(enum(latest.EnumWithOneMember).didnt_have("hoo"))
