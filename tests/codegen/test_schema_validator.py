import inspect
import re
from typing import Any

import pytest
from pydantic import root_validator, validator

from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure import schema
from tests.conftest import CreateLocalSimpleVersionedPackages


def test__schema_validator_existed(create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages):
    @root_validator(pre=True)
    def hewwo(cls, values):
        raise NotImplementedError

    @validator("foo")
    def dawkness(cls, value):
        raise NotImplementedError

    v1 = create_local_simple_versioned_packages(
        schema(head_with_one_str_field.SchemaWithOneStrField).validator(hewwo).existed,
        schema(head_with_one_str_field.SchemaWithOneStrField).validator(dawkness).existed,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == (
        "class SchemaWithOneStrField(BaseModel):\n"
        "    foo: str\n\n"
        "    @root_validator(pre=True)\n"
        "    def hewwo(cls, values):\n"
        "        raise NotImplementedError\n\n"
        "    @validator('foo')\n"
        "    def dawkness(cls, value):\n"
        "        raise NotImplementedError\n"
    )


@pytest.fixture()
def head_with_validator():
    return head_module_for(
        """
    from pydantic import BaseModel, validator
    class SchemaWithOneStrField(BaseModel):
        foo: str

        @validator("foo")
        def validate_foo(cls, value):
            return value
    """,
    )


def test__schema_validator_didnt_exist(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    v1 = create_local_simple_versioned_packages(
        schema(head_with_validator.SchemaWithOneStrField)
        .validator(head_with_validator.SchemaWithOneStrField.validate_foo)
        .didnt_exist,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    foo: str\n"


def test__schema_validator_didnt_exist__applied_twice__should_raise_error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    instruction = (
        schema(head_with_validator.SchemaWithOneStrField)
        .validator(head_with_validator.SchemaWithOneStrField.validate_foo)
        .didnt_exist
    )
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "validate_foo" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it is already deleted.'
        ),
    ):
        create_local_simple_versioned_packages(instruction, instruction)


def test__schema_validator_didnt_exist__for_nonexisting_validator__should_raise_error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    @validator("foo")
    def fake_validator(cls, value):
        raise NotImplementedError

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "fake_validator" from "SchemaWithOneStrField" in'
            ' "MyVersionChange" but it doesn\'t have such a validator.'
        ),
    ):
        create_local_simple_versioned_packages(
            schema(head_with_validator.SchemaWithOneStrField).validator(fake_validator).didnt_exist,
        )


def test__schema_validator_existed__non_validator_was_passed__should_raise_error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    def fake_validator(cls, value):
        raise NotImplementedError

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "fake_validator" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a validator.'
        ),
    ):
        create_local_simple_versioned_packages(
            schema(head_with_validator.SchemaWithOneStrField).validator(fake_validator).didnt_exist,
        )


def test__schema_field_didnt_exist__with_validator__validator_must_be_deleted_too(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    v1 = create_local_simple_versioned_packages(
        schema(head_with_validator.SchemaWithOneStrField).field("foo").didnt_exist,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    pass\n"


def test__schema_field_didnt_exist__with_validator_that_covers_multiple_fields__validator_loses_one_of_its_args(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
):
    latest = head_module_for(
        """
    from pydantic import BaseModel, validator

    class SchemaWithOneStrField(BaseModel):
        foo: str
        bar: str

        @validator("bar")
        def validate_bar(cls, value):
            return value

        @validator("foo", "bar")
        def validate_foo(cls, value):
            return value

    """,
    )
    v1 = create_local_simple_versioned_packages(
        schema(latest.SchemaWithOneStrField).field("foo").didnt_exist,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == (
        "class SchemaWithOneStrField(BaseModel):\n"
        "    bar: str\n\n"
        "    @validator('bar')\n"
        "    def validate_bar(cls, value):\n"
        "        return value\n\n"
        "    @validator('bar')\n"
        "    def validate_foo(cls, value):\n"
        "        return value\n"
    )
