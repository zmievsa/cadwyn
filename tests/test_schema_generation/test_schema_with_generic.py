import re
from typing import Annotated, Any, Generic, TypeVar, Union

import pytest
from pydantic import BaseModel, Field

from cadwyn import schema
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import PossibleFieldAttributes
from tests.conftest import (
    CreateRuntimeSchemas,
    assert_models_are_equal,
    version_change,
)
from tests.test_schema_generation.test_schema_field import assert_field_had_changes_apply

BoolT = TypeVar("BoolT", bound=Union[bool, int])


class GenericSchema(BaseModel, Generic[BoolT]):
    foo: BoolT
    bar: bool


class ParametrizedSchema(GenericSchema[bool]):
    pass


ATTR_MAP: tuple[tuple[str, str], list[tuple[PossibleFieldAttributes, Any]]] = (
    ("attr", "value"),
    [
        ("title", "Foo"),
        ("description", "A foo"),
        ("repr", False),
    ],
)

###########
# GENERIC #
###########


def test__schema_field_existed_as__field_is_typevar(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchema)
            .field("baz")
            .existed_as(
                type=BoolT,
            ),
        ),
    )

    class ExpectedSchema(BaseModel):
        foo: BoolT  # pyright: ignore[reportGeneralTypeIssues] # Generics are actually erased, but the type is passed through pydantic metadata.
        bar: bool

        baz: BoolT  # pyright: ignore[reportGeneralTypeIssues] # Generics are actually erased, but the type is passed through pydantic metadata.

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


def test__schema_field_didnt_exist__field_is_typevar(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchema).field("foo").didnt_exist,
        ),
    )

    class ExpectedSchema(BaseModel):
        bar: bool

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


@pytest.mark.parametrize(*ATTR_MAP)
def test__schema_field_had__modifying_typevar_field(
    attr: PossibleFieldAttributes,
    value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    assert_field_had_changes_apply(
        GenericSchema,
        attr,
        value,
        create_runtime_schemas,
    )


@pytest.mark.parametrize(*ATTR_MAP)
def test__schema_field_didnt_have__modifying_typevar_field(
    attr: PossibleFieldAttributes, value: Any, create_runtime_schemas: CreateRuntimeSchemas
):
    class GenericSchemaWithMetadata(BaseModel, Generic[BoolT]):
        foo: Annotated[BoolT, Field(**dict(ATTR_MAP[1]))]
        bar: bool

    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchemaWithMetadata).field("foo").didnt_have(attr),
        )
    )

    assert not schemas["2000-01-01"][GenericSchemaWithMetadata].model_fields["foo"].metadata


def test__schema_field_had__field_type_is_narrowed(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSchema)
            .field("bar")
            .had(
                type=BoolT,
            ),
        ),
    )

    class ExpectedSchema(BaseModel):
        foo: BoolT  # pyright: ignore[reportGeneralTypeIssues] # Generics are actually erased, but the type is passed through pydantic metadata.
        bar: BoolT  # pyright: ignore[reportGeneralTypeIssues] # Generics are actually erased, but the type is passed through pydantic metadata.

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


################
# PARAMETRIZED #
################


def test__schema_field_didnt_exist__parametrized_field_is_missing__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a field "foo" from "ParametrizedSchema" '
            'in "MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(
            version_change(
                schema(ParametrizedSchema).field("foo").didnt_exist,
            ),
        )


@pytest.mark.parametrize(*ATTR_MAP)
def test__schema_field_had__modifying_parametrized_field(
    attr: str,
    value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    assert_field_had_changes_apply(
        ParametrizedSchema,
        attr,
        value,
        create_runtime_schemas,
    )


def test__schema_field_had__field_parametrized_type_is_replaced(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParametrizedSchema)
            .field("foo")
            .had(
                type=int,
            ),
        ),
    )

    class ExpectedSchema(GenericSchema[int]):
        pass

    assert_models_are_equal(schemas["2000-01-01"][ParametrizedSchema], ExpectedSchema)
