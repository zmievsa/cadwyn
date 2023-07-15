import re
from datetime import date

import pytest

from tests._data.latest import SchemaWithOneIntField
from universi import Field, api_version_var
from universi.exceptions import UniversiStructureError
from universi.structure import AbstractVersionChange
from universi.structure.responses import convert_response_to_previous_version_for
from universi.structure.schemas import schema
from universi.structure.versions import Version, Versions


class DummySubClass2000_001(AbstractVersionChange):
    description = "dummy description"
    instructions_to_migrate_to_previous_version = []


class DummySubClass2000_002(AbstractVersionChange):
    description = "dummy description2"
    instructions_to_migrate_to_previous_version = []


class DummySubClass2001(AbstractVersionChange):
    description = "dummy description3"
    instructions_to_migrate_to_previous_version = []


class DummySubClass2002(AbstractVersionChange):
    description = "dummy description4"
    instructions_to_migrate_to_previous_version = []


@pytest.fixture()
def versions():
    return Versions(
        Version(date(2002, 1, 1), DummySubClass2002),
        Version(date(2001, 1, 1), DummySubClass2001),
        Version(date(2000, 1, 1), DummySubClass2000_001, DummySubClass2000_002),
        Version(date(1999, 1, 1)),
    )


def test_non_boolean_side_effects():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape("Side effects must be bool. Found: <class 'str'>"),
    ):

        class DummySubClass(AbstractVersionChange):
            side_effects = "not a boolean"
            description = "dummy description"
            instructions_to_migrate_to_previous_version = []


def test_description_sentinel():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Version change description is not set on 'DummySubClass' but is required.",
        ),
    ):

        class DummySubClass(AbstractVersionChange):
            instructions_to_migrate_to_previous_version = []


def test_instructions_sentinel():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Attribute 'instructions_to_migrate_to_previous_version' is not set on 'DummySubClass' but is required.",
        ),
    ):

        class DummySubClass(AbstractVersionChange):
            description = "dummy description"


def test_instructions_not_a_sequence():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Attribute 'instructions_to_migrate_to_previous_version' must be a sequence in 'DummySubClass'.",
        ),
    ):

        class DummySubClass(AbstractVersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = True


def test_non_instruction_attribute():
    with pytest.raises(
        UniversiStructureError,
        match=re.escape(
            "Found: 'dummy_attribute' attribute of type '<class 'str'>' in 'DummySubClass'. Only migration instructions and schema properties are allowed in version change class body."
        ),
    ):

        class DummySubClass(AbstractVersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = []
            dummy_attribute = "dummy attribute"


def test_incorrect_subclass_hierarchy():
    class DummySubClass(AbstractVersionChange):
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

        class DummySubClass(AbstractVersionChange):
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

        class DummySubClass(AbstractVersionChange):
            description = "dummy description"
            instructions_to_migrate_to_previous_version = [True]


def test_incorrectly_sorted_versions():
    with pytest.raises(
        ValueError,
        match=re.escape(
            "Versions are not sorted correctly. Please sort them in descending order.",
        ),
    ):
        Versions(Version(date(2000, 1, 1)), Version(date(2001, 1, 1)))


def test__is_active__var_is_none__everything_is_active(versions: Versions):
    api_version_var.set(None)
    assert versions.is_active(DummySubClass2002) is True
    assert versions.is_active(DummySubClass2001) is True
    assert versions.is_active(DummySubClass2000_001) is True
    assert versions.is_active(DummySubClass2000_002) is True


def test__is_active__var_is_later_than_latest__everything_is_active(versions: Versions):
    api_version_var.set(date(2003, 1, 1))
    assert versions.is_active(DummySubClass2002) is True
    assert versions.is_active(DummySubClass2001) is True
    assert versions.is_active(DummySubClass2000_001) is True
    assert versions.is_active(DummySubClass2000_002) is True


def test__is_active__var_is_before_latest__latest_is_inactive(versions: Versions):
    api_version_var.set(date(2001, 1, 1))
    assert versions.is_active(DummySubClass2002) is False
    assert versions.is_active(DummySubClass2001) is True
    assert versions.is_active(DummySubClass2000_001) is True
    assert versions.is_active(DummySubClass2000_002) is True


def test__is_active__var_is_at_earliest__everything_is_inactive(versions: Versions):
    api_version_var.set(date(1999, 3, 1))
    assert versions.is_active(DummySubClass2002) is False
    assert versions.is_active(DummySubClass2001) is False
    assert versions.is_active(DummySubClass2000_001) is False
    assert versions.is_active(DummySubClass2000_002) is False


def test__conversion_method__with_incorrect_structure():
    async def some_endpoint():
        raise NotImplementedError

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Method 'my_conversion_method' must have 2 parameters: cls and data",
        ),
    ):

        @convert_response_to_previous_version_for(some_endpoint)
        def my_conversion_method(cls, response):
            raise NotImplementedError

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Method 'my_conversion_method2' must have 2 parameters: cls and data",
        ),
    ):

        @convert_response_to_previous_version_for(some_endpoint)
        def my_conversion_method2():
            raise NotImplementedError
