import inspect
import re
from typing import Any

import pydantic
import pytest
from pydantic import root_validator, validator

from cadwyn._compat import PYDANTIC_V2
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure import schema
from tests.conftest import CreateLocalSimpleVersionedPackages, LatestModuleFor


def test__schema_validator_existed(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_one_str_field: Any,
):
    @root_validator(pre=True)
    def hewwo(cls, values):
        raise NotImplementedError

    @validator("foo")
    def dawkness(cls, value):
        raise NotImplementedError

    v1 = create_local_simple_versioned_packages(
        schema(latest_with_one_str_field.SchemaWithOneStrField).validator(hewwo).existed,
        schema(latest_with_one_str_field.SchemaWithOneStrField).validator(dawkness).existed,
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


def test__schema_validator_existed__with_root_validator_without_call(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_one_str_field: Any,
):
    if PYDANTIC_V2:
        pytest.skip("This test is only for Pydantic v1.")

    @pydantic.root_validator
    def hewwo(cls, values):
        raise NotImplementedError

    v1 = create_local_simple_versioned_packages(
        schema(latest_with_one_str_field.SchemaWithOneStrField).validator(hewwo).existed,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == (
        "class SchemaWithOneStrField(BaseModel):\n"
        "    foo: str\n\n"
        "    @pydantic.root_validator\n"
        "    def hewwo(cls, values):\n"
        "        raise NotImplementedError\n"
    )


@pytest.fixture()
def latest_with_validator(latest_module_for: LatestModuleFor):
    return latest_module_for(
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
    latest_with_validator: Any,
):
    v1 = create_local_simple_versioned_packages(
        schema(latest_with_validator.SchemaWithOneStrField)
        .validator(latest_with_validator.SchemaWithOneStrField.validate_foo)
        .didnt_exist,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    foo: str\n"


def test__schema_validator_didnt_exist__for_nonexisting_validator__should_raise_error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_validator: Any,
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
            schema(latest_with_validator.SchemaWithOneStrField).validator(fake_validator).didnt_exist,
        )


def test__schema_validator_existed__non_validator_was_passed__should_raise_error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_validator: Any,
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
            schema(latest_with_validator.SchemaWithOneStrField).validator(fake_validator).didnt_exist,
        )


def test__schema_field_didnt_exist__with_validator__validator_must_be_deleted_too(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_with_validator: Any,
):
    v1 = create_local_simple_versioned_packages(
        schema(latest_with_validator.SchemaWithOneStrField).field("foo").didnt_exist,
    )

    assert inspect.getsource(v1.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    pass\n"


def test__schema_field_didnt_exist__with_validator_that_covers_multiple_fields__validator_loses_one_of_its_args(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    latest_module_for: LatestModuleFor,
):
    latest = latest_module_for(
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
