import inspect
import re
from typing import Any

import pydantic
import pytest
from pydantic import BaseModel, root_validator, validator

from cadwyn._compat import PYDANTIC_V2
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure import schema
from tests.conftest import (
    CreateLocalSimpleVersionedPackages,
    CreateRuntimeSchemas,
    HeadModuleFor,
    assert_models_are_equal,
    version_change,
)


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str


def test__schema_validator_existed(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    @root_validator(pre=True)
    def hewwo(cls, values):
        raise NotImplementedError

    @validator("foo")
    def dawkness(cls, value):
        raise NotImplementedError

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField).validator(hewwo).existed,
            schema(SchemaWithOneStrField).validator(dawkness).existed,
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str

        @root_validator(pre=True)
        def hewwo(cls, values):
            raise NotImplementedError

        @validator("foo")
        def dawkness(cls, value):
            raise NotImplementedError

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)


class SchemaWithOneStrFieldAndValidator(BaseModel):
    foo: str

    @validator("foo")
    def validate_foo(cls, value):
        return value


def test__schema_validator_didnt_exist(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrFieldAndValidator)
            .validator(SchemaWithOneStrFieldAndValidator.validate_foo)
            .didnt_exist
        ),
    )

    class ExpectedSchema(BaseModel):
        foo: str

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrFieldAndValidator], ExpectedSchema)


def test__schema_validator_didnt_exist__applied_twice__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "validate_foo" from "SchemaWithOneStrFieldAndValidator" in '
            '"MyVersionChange" but it is already deleted.'
        ),
    ):
        create_runtime_schemas(
            version_change(
                schema(SchemaWithOneStrFieldAndValidator)
                .validator(SchemaWithOneStrFieldAndValidator.validate_foo)
                .didnt_exist,
                schema(SchemaWithOneStrFieldAndValidator)
                .validator(SchemaWithOneStrFieldAndValidator.validate_foo)
                .didnt_exist,
            )
        )


def test__schema_validator_didnt_exist__for_nonexisting_validator__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    @validator("foo")
    def fake_validator(cls, value):
        raise NotImplementedError

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "fake_validator" from "SchemaWithOneStrFieldAndValidator" in'
            ' "MyVersionChange" but it doesn\'t have such a validator.'
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrFieldAndValidator).validator(fake_validator).didnt_exist),
        )


def test__schema_validator_existed__non_validator_was_passed__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    def fake_validator(cls, value):
        raise NotImplementedError

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "fake_validator" from "SchemaWithOneStrFieldAndValidator" in '
            '"MyVersionChange" but it doesn\'t have such a validator.'
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrFieldAndValidator).validator(fake_validator).didnt_exist),
        )


def test__schema_field_didnt_exist__with_validator__validator_must_be_deleted_too(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithOneStrFieldAndValidator).field("foo").didnt_exist),
    )

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrFieldAndValidator], EmptySchema)


def test__schema_field_didnt_exist__with_validator_that_covers_multiple_fields__validator_loses_one_of_its_args(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithOneStrField(BaseModel):
        foo: str
        bar: str

        @validator("bar")
        def validate_bar(cls, value):
            return value

        @validator("foo", "bar")
        def validate_foo(cls, value):
            return value

    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").didnt_exist))

    class ExpectedSchema(BaseModel):
        bar: str

        @validator("bar")
        def validate_bar(cls, value):
            raise NotImplementedError

        @validator("bar")
        def validate_foo(cls, value):
            raise NotImplementedError

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)
