import re
from typing import Annotated, Any, Generic, TypeVar, Union, cast

import pytest
from annotated_types import Ge, Le, MaxLen
from pydantic import BaseModel, Field, ValidationError

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

    class ExpectedSchema(BaseModel, Generic[BoolT]):
        foo: BoolT
        bar: bool

        baz: BoolT

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

    class ExpectedSchema(BaseModel, Generic[BoolT]):
        foo: BoolT
        bar: BoolT

    assert_models_are_equal(schemas["2000-01-01"][GenericSchema], ExpectedSchema)


def test__schema_had_description__generic_model__should_only_change_description_of_generic_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(GenericSchema).had(description="Old generic description")))

    assert schemas["2000-01-01"][GenericSchema].__doc__ == "Old generic description"
    assert schemas["2000-01-01"][GenericSchema].model_json_schema()["description"] == "Old generic description"
    assert "description" not in schemas["2001-01-01"][GenericSchema].model_json_schema()
    # Like in pydantic, the subclasses of the generic model do not inherit its description
    assert "description" not in schemas["2000-01-01"][ParametrizedSchema].model_json_schema()


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


def test__schema_had_description__subclass_of_parametrized_model__should_change_its_description(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParametrizedSchema).had(description="Old parametrized description"))
    )

    assert schemas["2000-01-01"][ParametrizedSchema].model_json_schema()["description"] == (
        "Old parametrized description"
    )
    assert "description" not in schemas["2000-01-01"][GenericSchema].model_json_schema()
    assert_models_are_equal(schemas["2000-01-01"][ParametrizedSchema], ParametrizedSchema)


################################
# INHERITED FIELDS OF GENERICS #
################################

FirstT = TypeVar("FirstT")
SecondT = TypeVar("SecondT")


class GenericPair(BaseModel, Generic[FirstT, SecondT]):
    first: FirstT
    second: SecondT


class StrIntPair(GenericPair[str, int]):
    pass


class OtherStrIntPair(GenericPair[str, int]):
    pass


class PairBox(BaseModel, Generic[FirstT]):
    pair: GenericPair[FirstT, int]


class NonGenericParent(BaseModel):
    name: str
    count: int = 0


class GenericChildOfNonGenericParent(NonGenericParent, Generic[FirstT]):
    payload: FirstT


class NonGenericSiblingOfGenericChild(NonGenericParent):
    pass


def _get_constraints(model: type[BaseModel], field_name: str) -> list[Any]:
    return model.model_fields[field_name].metadata


def test__schema_field_had__inherited_field_of_subclass_of_parametrization__should_not_change_parents_and_siblings(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericPair).field("first").had(description="First"),
            schema(StrIntPair).field("second").had(ge=5),
        )
    )

    # Before #466, the change of StrIntPair also applied to the generic model, its parametrization and its siblings
    assert _get_constraints(schemas["2000-01-01"][StrIntPair], "second") == [Ge(5)]
    for model in (GenericPair, GenericPair[str, int], OtherStrIntPair):
        assert _get_constraints(schemas["2000-01-01"][model], "second") == []


def test__schema_field_had__inherited_field_of_parametrization__should_change_it_and_its_subclasses_only(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericPair).field("first").had(description="First"),
            schema(GenericPair[str, int]).field("second").had(ge=5),
            schema(StrIntPair).field("second").had(le=100),
        )
    )

    old_schemas = schemas["2000-01-01"]
    for model in (GenericPair[str, int], OtherStrIntPair):
        assert _get_constraints(old_schemas[model], "second") == [Ge(5)]
    assert _get_constraints(old_schemas[StrIntPair], "second") == [Ge(5), Le(100)]
    for model in (GenericPair, GenericPair[int, int]):
        assert _get_constraints(old_schemas[model], "second") == []
    # The instructions of the parametrization also apply where other generics build it (#420), but not the ones of
    # its subclasses
    pair_in_box = cast("type[BaseModel]", old_schemas[PairBox[str]].model_fields["pair"].annotation)
    assert _get_constraints(pair_in_box, "second") == [Ge(5)]
    assert old_schemas[PairBox[str]].model_validate({"pair": {"first": "a", "second": 500}}).pair.second == 500
    with pytest.raises(ValidationError):
        old_schemas[PairBox[str]].model_validate({"pair": {"first": "a", "second": 1}})


def test__schema_field_had__inherited_field_of_generic_subclass__should_change_its_parametrizations_only(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(NonGenericParent).field("count").had(description="Count"),
            schema(GenericChildOfNonGenericParent).field("name").had(max_length=3),
        )
    )

    old_schemas = schemas["2000-01-01"]
    for model in (GenericChildOfNonGenericParent, GenericChildOfNonGenericParent[int]):
        assert _get_constraints(old_schemas[model], "name") == [MaxLen(3)]
    for model in (NonGenericParent, NonGenericSiblingOfGenericChild):
        assert _get_constraints(old_schemas[model], "name") == []
