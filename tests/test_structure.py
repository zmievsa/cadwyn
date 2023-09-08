import re
from contextvars import ContextVar
from datetime import date
from typing import Any

import pytest
from pydantic import BaseModel

from universi.exceptions import UniversiError, UniversiStructureError
from universi.structure import VersionChange, VersionChangeWithSideEffects
from universi.structure.responses import convert_response_to_previous_version_for
from universi.structure.versions import Version, VersionBundle


class DummySubClass2000_001(VersionChangeWithSideEffects):
    description = "dummy description"
    instructions_to_migrate_to_previous_version = []


class DummySubClass2000_002(VersionChangeWithSideEffects):
    description = "dummy description2"
    instructions_to_migrate_to_previous_version = []


class DummySubClass2001(VersionChangeWithSideEffects):
    description = "dummy description3"
    instructions_to_migrate_to_previous_version = []


class DummySubClass2002(VersionChangeWithSideEffects):
    description = "dummy description4"
    instructions_to_migrate_to_previous_version = []


@pytest.fixture()
def dummy_sub_class_without_version():
    class DummySubClassWithoutVersion(VersionChangeWithSideEffects):
        description = "dummy description4"
        instructions_to_migrate_to_previous_version = []

    return DummySubClassWithoutVersion


@pytest.fixture()
def versions(api_version_var: ContextVar[date | None]):
    try:
        yield VersionBundle(
            Version(date(2002, 1, 1), DummySubClass2002),
            Version(date(2001, 1, 1), DummySubClass2001),
            Version(date(2000, 1, 1), DummySubClass2000_001, DummySubClass2000_002),
            Version(date(1999, 1, 1)),
            api_version_var=api_version_var,
        )
    finally:
        DummySubClass2002._bound_versions = None
        DummySubClass2001._bound_versions = None
        DummySubClass2000_001._bound_versions = None
        DummySubClass2000_002._bound_versions = None


def test_description_sentinel():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Version change description is not set on 'DummySubClass' but is required.",
        ),
    ):

        class DummySubClass(VersionChange):
            instructions_to_migrate_to_previous_version = []


def test_instructions_sentinel():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Attribute 'instructions_to_migrate_to_previous_version' is not set on 'DummySubClass' but is required.",
        ),
    ):

        class DummySubClass(VersionChange):
            description = "dummy description"


def test_instructions_not_a_sequence():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Attribute 'instructions_to_migrate_to_previous_version' must be a sequence in 'DummySubClass'.",
        ),
    ):

        class DummySubClass(VersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = True  # pyright: ignore[reportGeneralTypeIssues]


def test_non_instruction_attribute():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Found: 'dummy_attribute' attribute of type '<class 'str'>' in 'DummySubClass'."
            " Only migration instructions and schema properties are allowed in version change class body.",
        ),
    ):

        class DummySubClass(VersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = []
            dummy_attribute = "dummy attribute"


@pytest.mark.parametrize(
    "version_change_type",
    [VersionChange, VersionChangeWithSideEffects],
)
def test__incorrect_subclass_hierarchy(version_change_type: type[VersionChange]):
    class DummySubClass(version_change_type):
        description = "dummy description"
        instructions_to_migrate_to_previous_version = []

    with pytest.raises(
        TypeError,
        match=re.escape(
            "Can't subclass DummySubSubClass as it was never meant to be subclassed.",
        ),
    ):

        class DummySubSubClass(DummySubClass):
            pass


def test_instantiation_attempt():
    with pytest.raises(
        TypeError,
        match=re.escape(
            "Can't instantiate DummySubClass as it was never meant to be instantiated.",
        ),
    ):

        class DummySubClass(VersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = []

        DummySubClass()  # this will raise a TypeError


def test_invalid_type_in_instructions():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Instruction 'True' is not allowed. Please, use the correct instruction types",
        ),
    ):

        class DummySubClass(VersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = [True]  # pyright: ignore[reportGeneralTypeIssues]


def test_incorrectly_sorted_versions(api_version_var: ContextVar[date | None]):
    with pytest.raises(
        ValueError,
        match=re.escape(
            "Versions are not sorted correctly. Please sort them in descending order.",
        ),
    ):
        VersionBundle(
            Version(date(2000, 1, 1)),
            Version(date(2001, 1, 1)),
            api_version_var=api_version_var,
        )


def test__is_applied__var_is_none__everything_is_applied(
    versions: VersionBundle,
    api_version_var: ContextVar[date | None],
):
    assert DummySubClass2002.is_applied is True
    assert DummySubClass2001.is_applied is True
    assert DummySubClass2000_001.is_applied is True
    assert DummySubClass2000_002.is_applied is True


def test__is_applied__var_is_later_than_latest__everything_is_applied(
    versions: VersionBundle,
    api_version_var: ContextVar[date | None],
):
    api_version_var.set(date(2003, 1, 1))
    assert DummySubClass2002.is_applied is True
    assert DummySubClass2001.is_applied is True
    assert DummySubClass2000_001.is_applied is True
    assert DummySubClass2000_002.is_applied is True


def test__is_applied__var_is_before_latest__latest_is_inactive(
    versions: VersionBundle,
    api_version_var: ContextVar[date | None],
):
    api_version_var.set(date(2001, 1, 1))
    assert DummySubClass2002.is_applied is False
    assert DummySubClass2001.is_applied is True
    assert DummySubClass2000_001.is_applied is True
    assert DummySubClass2000_002.is_applied is True


def test__is_applied__var_is_at_earliest__everything_is_inactive(
    versions: VersionBundle,
    api_version_var: ContextVar[date | None],
):
    api_version_var.set(date(1999, 3, 1))
    assert DummySubClass2002.is_applied is False
    assert DummySubClass2001.is_applied is False
    assert DummySubClass2000_001.is_applied is False
    assert DummySubClass2000_002.is_applied is False


def test__is_applied__var_set_version_change_class_not_in_versions__error(
    dummy_sub_class_without_version: type[VersionChangeWithSideEffects],
    api_version_var: ContextVar[date | None],
):
    api_version_var.set(date(1999, 3, 1))
    with pytest.raises(
        UniversiError,
        match=re.escape(
            "You tried to check whether 'DummySubClassWithoutVersion' is active but it was never bound to any version.",
        ),
    ):
        assert dummy_sub_class_without_version.is_applied


def test__is_applied__var_unset_version_change_class_not_in_versions__error(
    dummy_sub_class_without_version: type[VersionChangeWithSideEffects],
    api_version_var: ContextVar[date | None],
):
    with pytest.raises(
        UniversiError,
        match=re.escape(
            "You tried to check whether 'DummySubClassWithoutVersion' is active but it was never bound to any version.",
        ),
    ):
        assert dummy_sub_class_without_version.is_applied


def test__versions__one_version_change_attached_to_two_version_bundles__error(
    dummy_sub_class_without_version: type[VersionChangeWithSideEffects],
    api_version_var: ContextVar[date | None],
):
    VersionBundle(
        Version(date(2000, 1, 1), dummy_sub_class_without_version),
        api_version_var=api_version_var,
    )
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "You tried to bind version change 'DummySubClassWithoutVersion' to two different versions."
            " It is prohibited.",
        ),
    ):
        VersionBundle(
            Version(date(2000, 1, 1), dummy_sub_class_without_version),
            api_version_var=api_version_var,
        )


def test__versions__one_version_change_attached_to_two_versions__error(
    dummy_sub_class_without_version: type[VersionChangeWithSideEffects],
    api_version_var: ContextVar[date | None],
):
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "You tried to bind version change 'DummySubClassWithoutVersion' to two different versions."
            " It is prohibited.",
        ),
    ):
        VersionBundle(
            Version(date(2001, 1, 1), dummy_sub_class_without_version),
            Version(date(2000, 1, 1), dummy_sub_class_without_version),
            api_version_var=api_version_var,
        )


def test__conversion_method__with_incorrect_structure():
    class SomeSchema(BaseModel):
        pass

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Method 'my_conversion_method' must have 2 parameters: cls and data",
        ),
    ):

        @convert_response_to_previous_version_for(SomeSchema)
        def my_conversion_method(cls: Any, response: Any):
            raise NotImplementedError

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Method 'my_conversion_method2' must have 2 parameters: cls and data",
        ),
    ):

        @convert_response_to_previous_version_for(SomeSchema)  # pyright: ignore[reportGeneralTypeIssues]
        def my_conversion_method2():
            raise NotImplementedError
