import re
from functools import partial, wraps
from typing import Any, Literal

import pytest
from pydantic import (
    BaseModel,
    field_serializer,
    field_validator,
    model_validator,
    root_validator,  # ty: ignore[deprecated]  # Tests intentionally cover Pydantic v1 validators.
    validator,  # ty: ignore[deprecated]  # Tests intentionally cover Pydantic v1 validators.
)

from cadwyn.exceptions import CadwynStructureError, InvalidGenerationInstructionError
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
    with pytest.warns(DeprecationWarning, match="Pydantic V1 style"):

        @root_validator(pre=True)  # ty: ignore[deprecated]  # Compatibility coverage for Pydantic v1 validators.
        def hewwo(cls, values):
            values["foo"] += "_root"
            return values

    with pytest.warns(DeprecationWarning, match="Pydantic V1 style"):

        @validator("foo")  # ty: ignore[deprecated]  # Compatibility coverage for Pydantic v1 validators.
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


@pytest.mark.parametrize("mode", ["before", "after", "plain", "wrap"])
@pytest.mark.parametrize("reference", ["attribute", "function", "name"])
def test__validator_didnt_exist__assigned_field_validator__should_remove_only_from_declaring_model_tree(
    create_runtime_schemas: CreateRuntimeSchemas,
    mode: Literal["before", "after", "plain", "wrap"],
    reference: str,
):
    def must_be_positive(value):
        if value <= 0:
            raise ValueError("must be positive")
        return value

    def must_be_positive_wrap(value, handler):
        return must_be_positive(handler(value))

    function = must_be_positive_wrap if mode == "wrap" else must_be_positive

    class Model(BaseModel):
        x: int
        check_x = field_validator("x", mode=mode)(function)

    class Child(Model):
        pass

    class Other(BaseModel):
        x: int
        check_x = field_validator("x", mode=mode)(function)

    references = {"attribute": Model.check_x, "function": function, "name": "check_x"}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x=-1).x == -1
    assert schemas["2000-01-01"][Child](x=-1).x == -1
    assert schemas["2001-01-01"][Model](x=1).x == 1
    for still_validated in (schemas["2000-01-01"][Other], schemas["2001-01-01"][Model], schemas["2001-01-01"][Child]):
        with pytest.raises(ValueError, match="must be positive"):
            still_validated(x=-1)


@pytest.mark.parametrize("mode", ["before", "after", "wrap"])
@pytest.mark.parametrize("reference", ["attribute", "function", "name"])
def test__validator_didnt_exist__assigned_model_validator__should_remove_only_from_older_version(
    create_runtime_schemas: CreateRuntimeSchemas,
    mode: Literal["before", "after", "wrap"],
    reference: str,
):
    def positive_before(values):
        if values["x"] <= 0:
            raise ValueError("must be positive")
        return values

    def positive_after(self):
        if self.x <= 0:
            raise ValueError("must be positive")
        return self

    def positive_wrap(cls, values, handler):
        return handler(positive_before(values))

    function, decorated = {
        "before": (positive_before, model_validator(mode="before")(positive_before)),
        "after": (positive_after, model_validator(mode="after")(positive_after)),
        "wrap": (positive_wrap, model_validator(mode="wrap")(positive_wrap)),
    }[mode]

    class Model(BaseModel):
        x: int
        check_model = decorated

    references = {"attribute": Model.check_model, "function": function, "name": "check_model"}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x=-1).x == -1
    assert schemas["2001-01-01"][Model](x=1).x == 1
    with pytest.raises(ValueError, match="must be positive"):
        schemas["2001-01-01"][Model](x=-1)


def test__validator_didnt_exist__assigned_helper_named_like_another_validator__should_remove_assigned_one(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    def check_b(cls, value):
        if value < 0:
            raise ValueError("a must be >= 0")
        return value

    helper_named_check_b = check_b

    class Model(BaseModel):
        a: int
        b: int
        check_a = field_validator("a")(helper_named_check_b)

        @field_validator("b")
        @classmethod
        def check_b(cls, value):
            if value < 0:
                raise ValueError("b must be >= 0")
            return value

    schemas = create_runtime_schemas(version_change(schema(Model).validator(Model.check_a).didnt_exist))

    assert schemas["2000-01-01"][Model](a=-1, b=0).a == -1
    with pytest.raises(ValueError, match="b must be >= 0"):
        schemas["2000-01-01"][Model](a=0, b=-1)
    assert schemas["2001-01-01"][Model](a=0, b=0).model_dump() == {"a": 0, "b": 0}
    with pytest.raises(ValueError, match="a must be >= 0"):
        schemas["2001-01-01"][Model](a=-1, b=0)


@pytest.mark.parametrize("wrapper", ["partial", "wraps"])
def test__validator_didnt_exist__wrapper_of_another_validators_function__should_remove_only_the_wrapper(
    create_runtime_schemas: CreateRuntimeSchemas, wrapper: str
):
    def normalize(value):
        return value + "!"

    @wraps(normalize)
    def wrapped_normalize(value):
        return normalize(value)

    wrapped = {"partial": partial(normalize), "wraps": wrapped_normalize}[wrapper]

    class Model(BaseModel):
        x: str
        y: str
        check_x = field_validator("x")(normalize)
        check_y = field_validator("y")(wrapped)

    schemas = create_runtime_schemas(version_change(schema(Model).validator(Model.check_y).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y"}
    assert schemas["2001-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y!"}


@pytest.mark.parametrize("binding", ["staticmethod", "classmethod"])
@pytest.mark.parametrize("reference", ["attribute", "descriptor"])
def test__validator_didnt_exist__partial_in_descriptor__should_preserve_other_registration(
    create_runtime_schemas: CreateRuntimeSchemas, binding: str, reference: str
):
    def normalize(value):
        return value + "!"

    def normalize_with_cls(cls, value):
        return normalize(value)

    wrapped = {
        "staticmethod": staticmethod(partial(normalize)),
        "classmethod": classmethod(partial(normalize_with_cls)),
    }[binding]

    class Model(BaseModel):
        x: str
        y: str
        check_x = field_validator("x")(normalize)
        check_y = field_validator("y")(wrapped)

    references = {"attribute": Model.check_y, "descriptor": Model.__dict__["check_y"]}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y"}
    assert schemas["2001-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y!"}


def test__validator_didnt_exist__different_bound_classes__should_only_remove_matching_registration(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Normalizer:
        @classmethod
        def normalize(cls, value):
            return value + "!"

    class First(Normalizer):
        pass

    class Second(Normalizer):
        pass

    class Model(BaseModel):
        x: str
        y: str
        check_x = field_validator("x")(staticmethod(First.normalize))
        check_y = field_validator("y")(staticmethod(Second.normalize))

    schemas = create_runtime_schemas(version_change(schema(Model).validator(Second.normalize).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y"}
    assert schemas["2001-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y!"}


def test__validator_didnt_exist__function_reused_for_fields__should_remove_only_named_registration(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    def normalize(value):
        return value.upper()

    class Model(BaseModel):
        x: str
        y: str
        z: str
        normalize_x = field_validator("x")(normalize)
        normalize_yz = field_validator("y", "z")(normalize)

    schemas = create_runtime_schemas(version_change(schema(Model).validator("normalize_yz").didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y", z="z").model_dump() == {"x": "X", "y": "y", "z": "z"}
    assert schemas["2001-01-01"][Model](x="x", y="y", z="z").model_dump() == {"x": "X", "y": "Y", "z": "Z"}


@pytest.mark.parametrize("reference", ["attribute", "function"])
@pytest.mark.parametrize("binding", ["plain", "classmethod", "classmethod_partial", "external_classmethod"])
def test__validator_didnt_exist__ambiguous_function__should_require_attribute_name(reference: str, binding: str):
    def normalize(value):
        return value.upper()

    def normalize_with_cls(cls, value):
        return normalize(value)

    class Normalizer:
        normalize = classmethod(normalize_with_cls)

    function = {
        "plain": normalize,
        "classmethod": normalize_with_cls,
        "classmethod_partial": classmethod(partial(normalize_with_cls)),
        "external_classmethod": staticmethod(Normalizer.normalize),
    }[binding]

    # The first attribute is named after the plain function, but only bound methods pick a validator by name
    class Model(BaseModel):
        x: str
        y: str
        normalize = field_validator("x")(function)
        normalize_y = field_validator("y")(function)

    assert Model(x="x", y="y").model_dump() == {"x": "X", "y": "Y"}
    references = {"attribute": Model.normalize_y, "function": function}
    with pytest.raises(
        CadwynStructureError,
        match=r'multiple validators on "Model": normalize, normalize_y.*validator\("normalize"\)',
    ):
        schema(Model).validator(references[reference]).didnt_exist


@pytest.mark.parametrize("reference", ["owner", "inherited"])
def test__validator_didnt_exist__existing_method_with_another_registration__should_keep_name_based_removal(
    create_runtime_schemas: CreateRuntimeSchemas, reference: str
):
    def normalize(cls, value):
        return value.upper()

    validator_x = field_validator("x")(normalize)
    validator_y = field_validator("y")(normalize)

    class Model(BaseModel):
        x: str
        y: str
        normalize = validator_x
        normalize_y = validator_y

    class Child(Model):
        pass

    references = {"owner": Model.normalize, "inherited": Child.normalize}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x", "y": "Y"}
    assert schemas["2000-01-01"][Child](x="x", y="y").model_dump() == {"x": "x", "y": "Y"}


@pytest.mark.parametrize("reference", ["attribute", "function", "name"])
def test__validator_didnt_exist__assigned_inherited_validator_on_child__should_raise_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas, reference: str
):
    def normalize(value):
        raise NotImplementedError

    class Parent(BaseModel):
        x: str
        normalize_x = field_validator("x")(normalize)

    class Child(Parent):
        pass

    references = {"attribute": Child.normalize_x, "function": normalize, "name": "normalize_x"}
    with pytest.raises(InvalidGenerationInstructionError, match='delete a validator "normalize_x" from "Child"'):
        create_runtime_schemas(version_change(schema(Child).validator(references[reference]).didnt_exist))


def test__validator_didnt_exist__method_from_another_model__should_keep_existing_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError, match='delete a validator "validate_foo"'):
        create_runtime_schemas(
            version_change(
                schema(SchemaWithOneStrField).validator(SchemaWithOneStrFieldAndValidator.validate_foo).didnt_exist
            )
        )


@pytest.mark.parametrize("name", ["missing", "serialize_x"])
def test__schema_validator__name_is_not_a_validator__should_raise_clear_error(name: str):
    class Model(BaseModel):
        x: str

        @field_serializer("x")
        def serialize_x(self, value):
            raise NotImplementedError

    with pytest.raises(CadwynStructureError, match=f'"Model" has no field or model validator named "{name}"'):
        schema(Model).validator(name)


@pytest.mark.parametrize("reference", ["attribute", "name"])
def test__validator_existed__validator_the_model_already_has__should_raise_clear_error(reference: str):
    def normalize(value):
        raise NotImplementedError

    class Model(BaseModel):
        x: str
        normalize_x = field_validator("x")(normalize)

    references = {"attribute": Model.normalize_x, "name": "normalize_x"}
    with pytest.raises(CadwynStructureError, match='"Model" already has the validator "normalize_x"'):
        schema(Model).validator(references[reference]).existed


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

        with pytest.warns(DeprecationWarning, match="Pydantic V1 style"):

            @validator("bar")  # ty: ignore[deprecated]  # Compatibility coverage for Pydantic v1 validators.
            def validate_bar(cls, value):
                raise NotImplementedError

        @field_validator("foo", "bar")
        def validate_foo(cls, value):
            raise NotImplementedError

    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").didnt_exist))

    class ExpectedSchema(BaseModel):
        bar: str

        @validator("bar")  # ty: ignore[deprecated]  # Compatibility coverage for Pydantic v1 validators.
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


def test__schema_generation__with_classmethod_field_serializer__generated_model_serializes_like_the_original(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithClassmethodFieldSerializer(BaseModel):
        foo: str

        @field_serializer("foo")
        @classmethod
        def serialize_foo(cls, value: str) -> str:
            return value.upper()

    schemas = create_runtime_schemas(version_change())
    original_dump = SchemaWithClassmethodFieldSerializer(foo="hello").model_dump()

    assert original_dump == {"foo": "HELLO"}
    assert schemas["2000-01-01"][SchemaWithClassmethodFieldSerializer](foo="hello").model_dump() == original_dump
    assert schemas["2001-01-01"][SchemaWithClassmethodFieldSerializer](foo="hello").model_dump() == original_dump


def test__schema_generation__with_classmethod_wrap_field_serializer__generated_model_serializes_like_the_original(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithClassmethodWrapFieldSerializer(BaseModel):
        foo: str

        @field_serializer("foo", mode="wrap")
        @classmethod
        def serialize_foo(cls, value: str, handler: Any) -> str:
            return handler(value) + "_serialized"

    schemas = create_runtime_schemas(version_change())
    original_dump = SchemaWithClassmethodWrapFieldSerializer(foo="hello").model_dump()

    assert original_dump == {"foo": "hello_serialized"}
    assert schemas["2000-01-01"][SchemaWithClassmethodWrapFieldSerializer](foo="hello").model_dump() == original_dump


def test__schema_generation__with_staticmethod_field_serializer__generated_model_serializes_like_the_original(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithStaticmethodFieldSerializer(BaseModel):
        foo: str

        @field_serializer("foo")
        @staticmethod
        def serialize_foo(value: str) -> str:
            return value.upper()

    schemas = create_runtime_schemas(version_change())
    original_dump = SchemaWithStaticmethodFieldSerializer(foo="hello").model_dump()

    assert original_dump == {"foo": "HELLO"}
    assert schemas["2000-01-01"][SchemaWithStaticmethodFieldSerializer](foo="hello").model_dump() == original_dump
    assert schemas["2001-01-01"][SchemaWithStaticmethodFieldSerializer](foo="hello").model_dump() == original_dump


def test__schema_generation__with_staticmethod_field_validator__generated_model_validates_like_the_original(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithStaticmethodFieldValidator(BaseModel):
        foo: str

        @field_validator("foo")
        @staticmethod
        def validate_foo(value: str) -> str:
            return value + "_validated"

    schemas = create_runtime_schemas(version_change())
    original_foo = SchemaWithStaticmethodFieldValidator(foo="hello").foo

    assert original_foo == "hello_validated"
    assert schemas["2000-01-01"][SchemaWithStaticmethodFieldValidator](foo="hello").foo == original_foo
    assert schemas["2001-01-01"][SchemaWithStaticmethodFieldValidator](foo="hello").foo == original_foo
