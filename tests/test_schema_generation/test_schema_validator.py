import re

import pytest
from pydantic import BaseModel, field_validator, model_validator, root_validator, validator

from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure import schema
from tests.conftest import (
    CreateRuntimeSchemas,
    assert_models_are_equal,
    version_change,
)


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str


def test__schema_validator_existed(create_runtime_schemas: CreateRuntimeSchemas):
    @model_validator(mode="before")
    def hewwo(cls, values):
        values["foo"] += "_root"
        return values

    @field_validator("foo")
    def dawkness(cls, value):
        return value + "_field"

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField).validator(hewwo).existed,
            schema(SchemaWithOneStrField).validator(dawkness).existed,
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str

        @model_validator(mode="before")
        def hewwo(cls, data):  # -> Any
            raise NotImplementedError

        @field_validator("foo")
        def dawkness(cls, value):
            raise NotImplementedError

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)
    assert schemas["2001-01-01"][SchemaWithOneStrField](foo="hello").foo == "hello"
    assert schemas["2000-01-01"][SchemaWithOneStrField](foo="hello").foo == "hello_root_field"


def test__schema_validator_existed__with_deprecated_validators(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.warns(DeprecationWarning):

        @root_validator(pre=True)
        def hewwo(cls, values):
            values["foo"] += "_root"
            return values

        @validator("foo")
        def dawkness(cls, value):
            return value + "_field"

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField).validator(hewwo).existed,
            schema(SchemaWithOneStrField).validator(dawkness).existed,
        )
    )

    assert schemas["2000-01-01"][SchemaWithOneStrField](foo="hello").foo == "hello_root_field"
    assert schemas["2001-01-01"][SchemaWithOneStrField](foo="hello").foo == "hello"


class SchemaWithOneStrFieldAndValidator(BaseModel):
    foo: str

    @field_validator("foo")
    def validate_foo(cls, value):
        raise NotImplementedError


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
    @field_validator("foo")
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
    @field_validator("foo")
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

        with pytest.warns(DeprecationWarning):

            @validator("bar")
            def validate_bar(cls, value):
                raise NotImplementedError

        @field_validator("foo", "bar")
        def validate_foo(cls, value):
            raise NotImplementedError

    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").didnt_exist))

    class ExpectedSchema(BaseModel):
        bar: str

        @validator("bar")
        def validate_bar(cls, value):
            raise NotImplementedError

        @field_validator("bar")
        def validate_foo(cls, value):
            raise NotImplementedError

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)


def test__schema_field_didnt_exist__with_validator_for_that_field_in_child(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Parent(BaseModel):
        foo: str

    class Child(Parent):
        @field_validator("foo", check_fields=False)
        def validate_foo(cls, value):
            raise NotImplementedError

    schemas = create_runtime_schemas(version_change(schema(Parent).field("foo").didnt_exist))
    parent = schemas["2000-01-01"][Parent]

    class ExpectedSchema(parent):
        pass

    assert_models_are_equal(schemas["2000-01-01"][Child], ExpectedSchema)
    assert schemas["2000-01-01"][Child](foo="hewwo")


def test__validator_didnt_exist__with_validator_defined_in_parent__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class A(BaseModel):
        foo: str

        @field_validator("foo")
        def validate_foo(cls, value):
            raise NotImplementedError

    class B(A):
        pass

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a validator "validate_foo" from "B" in '
            '"MyVersionChange" but it doesn\'t have such a validator.'
        ),
    ):
        create_runtime_schemas(version_change(schema(B).validator(A.validate_foo).didnt_exist))
