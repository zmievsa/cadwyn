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
def test__validator_didnt_exist__assigned_field_validator__should_remove_only_from_older_version(
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

    references = {"attribute": Model.check_x, "function": function, "name": "check_x"}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x=-1).x == -1
    assert schemas["2000-01-01"][Child](x=-1).x == -1
    assert schemas["2001-01-01"][Model](x=1).x == 1
    with pytest.raises(ValueError, match="must be positive"):
        schemas["2001-01-01"][Model](x=-1)
    with pytest.raises(ValueError, match="must be positive"):
        schemas["2001-01-01"][Child](x=-1)
    with pytest.raises(ValueError, match="must be positive"):
        Model(x=-1)


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

    class Child(Model):
        pass

    references = {"attribute": Model.check_model, "function": function, "name": "check_model"}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x=-1).x == -1
    assert schemas["2000-01-01"][Child](x=-1).x == -1
    assert schemas["2001-01-01"][Model](x=1).x == 1
    with pytest.raises(ValueError, match="must be positive"):
        schemas["2001-01-01"][Model](x=-1)
    with pytest.raises(ValueError, match="must be positive"):
        schemas["2001-01-01"][Child](x=-1)
    with pytest.raises(ValueError, match="must be positive"):
        Model(x=-1)


@pytest.mark.parametrize("reference", ["attribute", "function", "name"])
def test__validator_didnt_exist__assigned_classmethod__should_resolve_attribute_name(
    create_runtime_schemas: CreateRuntimeSchemas, reference: str
):
    def normalize(cls, value):
        return value.upper()

    class Model(BaseModel):
        x: str
        normalize_x = field_validator("x")(normalize)

    references = {"attribute": Model.normalize_x, "function": normalize, "name": "normalize_x"}
    schemas = create_runtime_schemas(version_change(schema(Model).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Model](x="hello").x == "hello"
    assert schemas["2001-01-01"][Model](x="hello").x == "HELLO"


@pytest.mark.parametrize("other_registration", ["function", "partial"])
def test__validator_didnt_exist__partial_registration__should_remove_partial_without_removing_underlying_function(
    create_runtime_schemas: CreateRuntimeSchemas, other_registration: str
):
    def normalize(value):
        return value + "!"

    other = {"function": normalize, "partial": partial(normalize)}[other_registration]

    class Model(BaseModel):
        x: str
        y: str
        check_x = field_validator("x")(other)
        check_y = field_validator("y")(partial(normalize))

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


def test__validator_didnt_exist__wraps_decorator__should_match_wrapper_identity(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    def normalize(value):
        return value + "!"

    @wraps(normalize)
    def decorated(value):
        return normalize(value) + "?"

    class Model(BaseModel):
        x: str
        y: str
        check_x = field_validator("x")(normalize)
        check_y = field_validator("y")(decorated)

    schemas = create_runtime_schemas(version_change(schema(Model).validator(Model.check_y).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y"}
    assert schemas["2001-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y!?"}


def test__schema_validator__unregistered_bound_instance__should_not_match_another_instance():
    class Normalizer:
        def __init__(self, suffix: str):
            self.suffix = suffix

        def normalize(self, value):
            return value + self.suffix

    first, second = Normalizer("!"), Normalizer("?")

    class Model(BaseModel):
        x: str
        check_x = field_validator("x")(staticmethod(first.normalize))

    assert Model(x="x").x == "x!"
    with pytest.raises(CadwynStructureError, match="The passed function must be a pydantic validator"):
        schema(Model).validator(second.normalize).didnt_exist


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


def test__schema_validator__unregistered_partial__should_not_match_underlying_function():
    def normalize(value):
        return value.upper()

    class Model(BaseModel):
        x: str
        check_x = field_validator("x")(normalize)

    assert Model(x="hello").x == "HELLO"
    with pytest.raises(CadwynStructureError, match="The passed function must be a pydantic validator"):
        schema(Model).validator(partial(normalize)).didnt_exist


@pytest.mark.parametrize("binding", ["plain", "classmethod"])
def test__schema_validator__partial_reused_in_registrations__should_require_attribute_name(binding: str):
    def normalize(value):
        return value + "!"

    def normalize_with_cls(cls, value):
        return normalize(value)

    wrapped = {"plain": partial(normalize), "classmethod": classmethod(partial(normalize_with_cls))}[binding]

    class Model(BaseModel):
        x: str
        y: str
        check_x = field_validator("x")(wrapped)
        check_y = field_validator("y")(wrapped)

    assert Model(x="x", y="y").model_dump() == {"x": "x!", "y": "y!"}
    with pytest.raises(CadwynStructureError, match='multiple validators on "Model": check_x, check_y'):
        schema(Model).validator(Model.check_y).didnt_exist


@pytest.mark.parametrize("reference", ["attribute", "function", "name"])
@pytest.mark.parametrize("binding", ["plain", "classmethod"])
def test__validator_didnt_exist__function_reused_in_models_and_inheritance__should_only_change_declaring_model_tree(
    create_runtime_schemas: CreateRuntimeSchemas, reference: str, binding: str
):
    def normalize(value):
        return value.upper()

    def normalize_with_cls(cls, value):
        return normalize(value)

    function = normalize_with_cls if binding == "classmethod" else normalize

    class Parent(BaseModel):
        x: str
        normalize_x = field_validator("x")(function)

    class Child(Parent):
        pass

    class Other(BaseModel):
        x: str
        check_x = field_validator("x")(function)

    references = {"attribute": Child.normalize_x, "function": function, "name": "normalize_x"}
    schemas = create_runtime_schemas(version_change(schema(Parent).validator(references[reference]).didnt_exist))

    assert schemas["2000-01-01"][Parent](x="hello").x == "hello"
    assert schemas["2000-01-01"][Child](x="hello").x == "hello"
    assert schemas["2000-01-01"][Other](x="hello").x == "HELLO"
    assert schemas["2001-01-01"][Child](x="hello").x == "HELLO"
    assert Parent(x="hello").x == "HELLO"
    assert Child(x="hello").x == "HELLO"


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
@pytest.mark.parametrize("binding", ["plain", "classmethod", "external_classmethod"])
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
        "external_classmethod": staticmethod(Normalizer.normalize),
    }[binding]

    class Model(BaseModel):
        x: str
        y: str
        normalize_x = field_validator("x")(function)
        normalize_y = field_validator("y")(function)

    assert Model(x="x", y="y").model_dump() == {"x": "X", "y": "Y"}
    references = {"attribute": Model.normalize_x, "function": function}
    with pytest.raises(
        CadwynStructureError,
        match=r'multiple validators on "Model": normalize_x, normalize_y.*validator\("normalize_x"\)',
    ):
        schema(Model).validator(references[reference]).didnt_exist


@pytest.mark.parametrize("reference", ["attribute", "function", "name"])
def test__validator_didnt_exist__assigned_inherited_validator_on_child__should_keep_existing_error(
    create_runtime_schemas: CreateRuntimeSchemas, reference: str
):
    def normalize(value):
        return value.upper()

    class Parent(BaseModel):
        x: str
        normalize_x = field_validator("x")(normalize)

    class Child(Parent):
        pass

    class Sibling(Parent):
        pass

    references = {"attribute": Child.normalize_x, "function": normalize, "name": "normalize_x"}
    assert Child(x="hello").x == "HELLO"
    assert Parent(x="hello").x == "HELLO"
    assert Sibling(x="hello").x == "HELLO"
    with pytest.raises(InvalidGenerationInstructionError, match='delete a validator "normalize_x" from "Child"'):
        create_runtime_schemas(version_change(schema(Child).validator(references[reference]).didnt_exist))
    assert Parent(x="hello").x == "HELLO"
    assert Sibling(x="hello").x == "HELLO"
    unchanged = create_runtime_schemas(version_change())
    assert unchanged["2000-01-01"][Parent](x="hello").x == "HELLO"
    assert unchanged["2000-01-01"][Child](x="hello").x == "HELLO"
    assert unchanged["2000-01-01"][Sibling](x="hello").x == "HELLO"


@pytest.mark.parametrize("name", ["missing", "serialize_x"])
def test__schema_validator__name_is_not_a_validator__should_raise_clear_error(name: str):
    class Model(BaseModel):
        x: str

        @field_serializer("x")
        def serialize_x(self, value):
            raise NotImplementedError

    with pytest.raises(CadwynStructureError, match=f'"Model" has no field or model validator named "{name}"'):
        schema(Model).validator(name)


def test__validator_existed__attribute_name__should_raise_clear_error():
    def normalize(value):
        raise NotImplementedError

    class Model(BaseModel):
        x: str
        normalize_x = field_validator("x")(normalize)

    with pytest.raises(CadwynStructureError, match="Adding validators by attribute name is not supported"):
        schema(Model).validator("normalize_x").existed


def test__validator_didnt_exist__existing_method_with_another_registration__should_keep_name_based_removal(
    create_runtime_schemas: CreateRuntimeSchemas,
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

    schemas = create_runtime_schemas(version_change(schema(Model).validator(Model.normalize).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x", "y": "Y"}


def test__validator_didnt_exist__method_from_another_model__should_keep_existing_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError, match='delete a validator "validate_foo"'):
        create_runtime_schemas(
            version_change(
                schema(SchemaWithOneStrField).validator(SchemaWithOneStrFieldAndValidator.validate_foo).didnt_exist
            )
        )


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


def test__validator_didnt_exist__partial_with_copied_method_name__should_remove_partial_registration(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    def normalize(cls, value):
        return value + "!"

    method_validator = field_validator("x")(normalize)
    wrapped_partial = wraps(normalize)(partial(normalize))

    class Model(BaseModel):
        x: str
        y: str
        normalize = method_validator
        check_y = field_validator("y")(classmethod(wrapped_partial))

    schemas = create_runtime_schemas(version_change(schema(Model).validator(Model.check_y).didnt_exist))

    assert schemas["2000-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y"}
    assert schemas["2001-01-01"][Model](x="x", y="y").model_dump() == {"x": "x!", "y": "y!"}
