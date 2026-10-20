import re
from decimal import Decimal
from enum import Enum, auto
from typing import Annotated, Any, ClassVar, Generic, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    computed_field,
    conint,
    constr,
)
from pydantic._internal._known_annotated_metadata import collect_known_metadata
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import override

from cadwyn.exceptions import (
    CadwynStructureError,
    InvalidGenerationInstructionError,
)
from cadwyn.schema_generation import SchemaGenerator
from cadwyn.structure import schema
from cadwyn.structure.schemas import PossibleFieldAttributes
from tests._resources.utils import HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED
from tests.conftest import (
    CreateRuntimeSchemas,
    assert_models_are_equal,
    version_change,
)

_T = TypeVar("_T")
_U = TypeVar("_U")


class MyEnum(Enum):
    foo = auto()
    baz = auto()


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str


class SchemaWithOneIntField(BaseModel):
    foo: int


##############
# EXISTED AS #
##############


def test__schema_field_existed_as__original_schema_is_empty(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(EmptySchema).field("bar").existed_as(type=int, info=Field(alias="boo")))
    )

    class ExpectedSchema(BaseModel):
        bar: int = Field(alias="boo")

    assert_models_are_equal(schemas["2000-01-01"][EmptySchema], ExpectedSchema)


def test__field_existed_as__original_schema_has_a_field(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField)
            .field("bar")
            .existed_as(type=int, info=Field(description="Hello darkness my old friend")),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str
        bar: int = Field(description="Hello darkness my old friend")

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)


def test__field_existed_as__extras_are_added(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.warns(DeprecationWarning, match="Using extra keyword arguments on"):
        schemas = create_runtime_schemas(
            version_change(
                schema(EmptySchema)
                .field("foo")
                .existed_as(
                    type=int,
                    info=Field(deflolbtt="hewwo"),
                ),
            )
        )

    class ExpectedSchema(BaseModel):
        foo: int = Field(json_schema_extra={"deflolbtt": "hewwo"})

    assert_models_are_equal(schemas["2000-01-01"][EmptySchema], ExpectedSchema)


def test__schema_field_existed_as__with_default_none(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(EmptySchema).field("foo").existed_as(type=Union[str, None], info=Field(default=None)),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: Union[str, None] = Field(default=None)

    assert_models_are_equal(schemas["2000-01-01"][EmptySchema], ExpectedSchema)


def test__schema_field_existed_as__with_new_weird_data_types(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(
            schema(EmptySchema).field("foo").existed_as(type=dict[str, int], info=Field(default={"a": "b"})),
            schema(EmptySchema)
            .field("bar")
            .existed_as(type=list[int], info=Field(default_factory=lambda: 83)),  # pragma: no cover
            schema(EmptySchema).field("baz").existed_as(type=Literal[MyEnum.foo]),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: dict[str, int] = Field(default={"a": "b"})  # ty: ignore[invalid-assignment]
        bar: list[int] = Field(default_factory=lambda: 83)  # pragma: no branch # ty: ignore[invalid-assignment]
        baz: Literal[MyEnum.foo]

    assert_models_are_equal(schemas["2000-01-01"][EmptySchema], ExpectedSchema)


################
# DIDN'T EXIST #
################


def test__schema_field_didnt_exist(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").didnt_exist))

    class ExpectedSchema(BaseModel):
        pass

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)


class ParentSchema(BaseModel):
    foo: str
    baz: int


class ChildSchema(ParentSchema):
    pass


def test__schema_field_didnt_exist__with_inheritance(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(ParentSchema).field("foo").didnt_exist),
        version_change(schema(ChildSchema).field("bar").existed_as(type=int)),
    )

    class ExpectedChildSchema(schemas["2000-01-01"][ParentSchema]):
        bar: int

    class ExpectedParentSchema(BaseModel):
        baz: int

    assert_models_are_equal(schemas["2000-01-01"][ParentSchema], ExpectedParentSchema)
    assert_models_are_equal(schemas["2000-01-01"][ChildSchema], ExpectedChildSchema)
    assert set(schemas["2000-01-01"][ChildSchema].model_fields) == {"bar", "baz"}


def test__schema_field_didnt_exist__with_inheritance_and_child_not_versioned__child_must_still_change(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(ParentSchema).field("foo").didnt_exist))

    class ExpectedParentSchema(BaseModel):
        baz: int

    assert_models_are_equal(schemas["2000-01-01"][ParentSchema], ExpectedParentSchema)
    assert set(schemas["2000-01-01"][ChildSchema].model_fields) == {"baz"}
    assert schemas["2000-01-01"][ChildSchema](baz=83)  # ty: ignore[missing-argument]


class InheritedFieldParent(BaseModel):
    foo: str = Field(default="", description="Head", max_length=10)
    bar: int = 0


class InheritedFieldChild(InheritedFieldParent):
    pass


class InheritedFieldSibling(InheritedFieldParent):
    pass


class InheritedFieldGrandchild(InheritedFieldChild):
    pass


class InheritedFieldOtherParent(InheritedFieldParent):
    pass


class InheritedFieldDiamond(InheritedFieldChild, InheritedFieldOtherParent):
    pass


class InheritedFieldMixin(BaseModel):
    pass


class InheritedFieldDiamondWithMixin(InheritedFieldMixin, InheritedFieldChild, InheritedFieldOtherParent):
    pass


class InheritedFieldRedeclaringChild(InheritedFieldParent):
    foo: str = Field(default="", description="Redeclared")


class InheritedFieldDiamondWithRedeclaredField(InheritedFieldRedeclaringChild, InheritedFieldOtherParent):
    baz: int = 0


def _get_description_and_max_length(model: type[BaseModel]) -> tuple[Union[str, None], Union[int, None]]:
    field = model.model_fields["foo"]
    max_length = next((m.max_length for m in field.metadata if hasattr(m, "max_length")), None)
    return field.description, max_length


# The parent has an instruction so that its field is the same object as the field of its subclasses in Cadwyn
PARENT_INSTRUCTION = schema(InheritedFieldParent).field("bar").had(description="Bar")


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected"),
    [
        pytest.param(
            [[schema(InheritedFieldChild).field("foo").had(description="Child", max_length=3)]],
            {
                InheritedFieldParent: ("Head", 10),
                InheritedFieldSibling: ("Head", 10),
                InheritedFieldOtherParent: ("Head", 10),
                InheritedFieldChild: ("Child", 3),
                InheritedFieldGrandchild: ("Child", 3),
                InheritedFieldDiamond: ("Child", 3),
            },
            id="child_had",
        ),
        pytest.param(
            [[schema(InheritedFieldChild).field("foo").didnt_have("description", "max_length")]],
            {
                InheritedFieldParent: ("Head", 10),
                InheritedFieldSibling: ("Head", 10),
                InheritedFieldChild: (None, None),
                InheritedFieldGrandchild: (None, None),
            },
            id="child_didnt_have",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(description="Changed"),
                    schema(InheritedFieldSibling).field("foo").had(description="Changed"),
                ]
            ],
            {
                InheritedFieldParent: ("Head", 10),
                InheritedFieldOtherParent: ("Head", 10),
                InheritedFieldChild: ("Changed", 10),
                InheritedFieldSibling: ("Changed", 10),
            },
            id="child_and_sibling_had_the_same_change",
        ),
        pytest.param(
            [
                [schema(InheritedFieldGrandchild).field("foo").had(description="Grandchild")],
                [schema(InheritedFieldChild).field("foo").had(max_length=3)],
            ],
            {
                InheritedFieldParent: ("Head", 10),
                InheritedFieldChild: ("Head", 3),
                InheritedFieldGrandchild: ("Grandchild", 3),
            },
            id="grandchild_then_child",
        ),
        pytest.param(
            [
                [schema(InheritedFieldGrandchild).field("foo").had(description="Grandchild")],
                [schema(InheritedFieldParent).field("foo").had(max_length=3)],
            ],
            {
                InheritedFieldParent: ("Head", 3),
                InheritedFieldSibling: ("Head", 3),
                InheritedFieldChild: ("Head", 3),
                InheritedFieldGrandchild: ("Grandchild", 3),
            },
            id="grandchild_then_parent",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [schema(InheritedFieldParent).field("foo").had(description="Parent")],
            ],
            {InheritedFieldParent: ("Parent", 10), InheritedFieldChild: ("Parent", 10)},
            id="child_then_parent_same_attribute",
        ),
        pytest.param(
            [
                [schema(InheritedFieldDiamond).field("foo").had(description="Diamond")],
                [schema(InheritedFieldOtherParent).field("foo").had(max_length=3)],
            ],
            {
                InheritedFieldChild: ("Head", 10),
                InheritedFieldOtherParent: ("Head", 3),
                InheritedFieldDiamond: ("Diamond", 3),
            },
            id="diamond_then_other_parent",
        ),
        pytest.param(
            # Pydantic takes the field of the diamond from its first base which does not have the change
            [[schema(InheritedFieldOtherParent).field("foo").had(max_length=3)]],
            {
                InheritedFieldChild: ("Head", 10),
                InheritedFieldOtherParent: ("Head", 3),
                InheritedFieldDiamond: ("Head", 10),
                InheritedFieldDiamondWithMixin: ("Head", 10),
            },
            id="other_parent_of_diamond",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [schema(InheritedFieldOtherParent).field("foo").had(max_length=3)],
            ],
            {
                InheritedFieldChild: ("Child", 10),
                InheritedFieldOtherParent: ("Head", 3),
                InheritedFieldDiamond: ("Child", 10),
            },
            id="both_parents_of_diamond",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldDiamondWithRedeclaredField).field("baz").had(description="Baz"),
                    schema(InheritedFieldOtherParent).field("foo").had(max_length=3),
                ]
            ],
            {InheritedFieldOtherParent: ("Head", 3), InheritedFieldDiamondWithRedeclaredField: ("Redeclared", None)},
            id="other_parent_of_diamond_whose_first_base_redeclares_the_field",
        ),
    ],
)
def test__schema_field_had__inherited_field__should_change_the_model_and_its_subclasses_only(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected: dict[type[BaseModel], tuple[Union[str, None], Union[int, None]]],
):
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest[1:])),
        version_change(PARENT_INSTRUCTION, *instructions_from_newest[0]),
    )

    assert {model: _get_description_and_max_length(schemas["2000-01-01"][model]) for model in expected} == expected


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected_child", "expected_sibling"),
    [
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(description="Child"),
                    schema(InheritedFieldSibling).field("foo").had(description="Head"),
                ]
            ],
            ("Child", 10),
            ("Head", 10),
            id="had_value_that_only_a_sibling_changed",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [schema(InheritedFieldSibling).field("foo").had(description="Head")],
            ],
            ("Child", 10),
            ("Head", 10),
            id="had_value_that_a_sibling_changed_in_a_newer_version",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(min_length=3),
                    schema(InheritedFieldSibling).field("foo").didnt_have("min_length"),
                ]
            ],
            ("Head", 10),
            ("Head", 10),
            id="didnt_have_attribute_that_only_a_sibling_had",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(type=bytes),
                    schema(InheritedFieldSibling).field("foo").had(type=str),
                ]
            ],
            ("Head", 10),
            ("Head", 10),
            id="had_type_that_only_a_sibling_changed",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").didnt_have("description")],
                [schema(InheritedFieldSibling).field("foo").had(description="Head")],
            ],
            (None, 10),
            ("Head", 10),
            id="had_value_after_a_sibling_deleted_it",
        ),
    ],
)
def test__schema_field_had__inherited_field_that_a_sibling_changed__should_accept_the_same_instructions_as_before(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected_child: tuple[Union[str, None], Union[int, None]],
    expected_sibling: tuple[Union[str, None], Union[int, None]],
):
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest[1:])),
        version_change(PARENT_INSTRUCTION, *instructions_from_newest[0]),
    )

    assert _get_description_and_max_length(schemas["2000-01-01"][InheritedFieldChild]) == expected_child
    assert _get_description_and_max_length(schemas["2000-01-01"][InheritedFieldSibling]) == expected_sibling
    assert _get_description_and_max_length(schemas["2000-01-01"][InheritedFieldParent]) == ("Head", 10)


@pytest.mark.parametrize(
    "instructions",
    [
        pytest.param(
            [
                schema(InheritedFieldChild).field("foo").had(type=bytes),
                schema(InheritedFieldSibling).field("foo").had(type=bytes),
            ],
            id="sibling",
        ),
        pytest.param(
            [
                schema(InheritedFieldChild).field("foo").had(type=bytes),
                schema(InheritedFieldParent).field("foo").had(type=bytes),
            ],
            id="parent",
        ),
    ],
)
def test__schema_field_had__type_that_only_another_model_had__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas, instructions: list[Any]
):
    # Before #466, Cadwyn rejected the second instruction because the first one also changed the type of the other
    # models that share the field
    schemas = create_runtime_schemas(version_change(PARENT_INSTRUCTION, *instructions))

    changed_models = {instruction.schema for instruction in instructions}
    for model in (InheritedFieldParent, InheritedFieldChild, InheritedFieldSibling):
        expected_type = bytes if model in changed_models or InheritedFieldParent in changed_models else str
        assert schemas["2000-01-01"][model].model_fields["foo"].annotation is expected_type


def test__schema_field_had__type_that_the_model_already_has_after_sibling_changed_it__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "foo" to "<class \'bytes\'>" from "InheritedFieldChild" in '
            '"MyVersionChange" but it already has type "<class \'bytes\'>"'
        ),
    ):
        create_runtime_schemas(
            version_change(
                PARENT_INSTRUCTION,
                schema(InheritedFieldChild).field("foo").had(type=bytes),
                schema(InheritedFieldSibling).field("foo").had(type=bytes),
                schema(InheritedFieldChild).field("foo").had(type=bytes),
            )
        )


class AnnotatedInheritedFieldParent(BaseModel):
    foo: Annotated[str, Field(description="Head")] = ""
    bar: int = 0


class AnnotatedInheritedFieldChild(AnnotatedInheritedFieldParent):
    pass


class AnnotatedInheritedFieldOtherChild(AnnotatedInheritedFieldParent):
    pass


def test__schema_field_didnt_have__parent_deleted_attribute_again_after_its_subclasses_changed_it__should_delete_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(AnnotatedInheritedFieldParent).field("foo").didnt_have("description")),
        version_change(schema(AnnotatedInheritedFieldOtherChild).field("foo").didnt_have("description")),
        version_change(
            schema(AnnotatedInheritedFieldParent).field("bar").had(description="Bar"),
            schema(AnnotatedInheritedFieldParent).field("foo").didnt_have("description"),
            schema(AnnotatedInheritedFieldChild).field("foo").had(description="Child"),
        ),
    )

    assert schemas["2002-01-01"][AnnotatedInheritedFieldChild].model_fields["foo"].description == "Child"
    assert schemas["2000-01-01"][AnnotatedInheritedFieldChild].model_fields["foo"].description is None


class GenericParentWithInheritedField(BaseModel, Generic[_T]):
    kind: _T
    bar: int = 0


class GenericChildWithInheritedField(GenericParentWithInheritedField[Literal["a"]]):
    pass


class GenericOtherParentWithInheritedField(GenericParentWithInheritedField[Literal["a"]]):
    pass


class GenericDiamondWithInheritedField(GenericChildWithInheritedField, GenericOtherParentWithInheritedField):
    pass


class OtherKind(BaseModel):
    kind: Literal["b"]


class EnvelopeWithGenericDiamond(BaseModel):
    item: Annotated[Union[GenericDiamondWithInheritedField, OtherKind], Field(discriminator="kind")]


def test__schema_field_had__generic_field_of_other_parent_of_diamond__should_keep_its_concrete_type(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericParentWithInheritedField).field("bar").had(description="Bar"),
            schema(GenericOtherParentWithInheritedField).field("kind").had(description="Kind"),
        )
    )

    diamond_field = schemas["2000-01-01"][GenericDiamondWithInheritedField].model_fields["kind"]
    assert diamond_field.annotation == Literal["a"]
    envelope = schemas["2000-01-01"][EnvelopeWithGenericDiamond]
    assert envelope.model_validate({"item": {"kind": "a"}}).item.kind == "a"
    with pytest.raises(ValidationError):
        envelope.model_validate({"item": {"kind": "c"}})


class InheritedItem(BaseModel):
    name: str


class ModelGenericParentWithInheritedField(BaseModel, Generic[_T]):
    value: _T
    bar: int = 0


class ModelGenericChildWithInheritedField(ModelGenericParentWithInheritedField[InheritedItem]):
    pass


class ModelGenericOtherParentWithInheritedField(ModelGenericParentWithInheritedField[InheritedItem]):
    pass


class ModelGenericDiamondWithInheritedField(
    ModelGenericChildWithInheritedField, ModelGenericOtherParentWithInheritedField
):
    pass


class QuotedGenericChildWithInheritedField(ModelGenericParentWithInheritedField["InheritedItem"]):
    pass


class QuotedGenericOtherParentWithInheritedField(ModelGenericParentWithInheritedField["InheritedItem"]):
    pass


class QuotedGenericDiamondWithInheritedField(
    QuotedGenericChildWithInheritedField, QuotedGenericOtherParentWithInheritedField
):
    pass


def test__schema_field_had__model_type_arg_field_of_other_parent_of_diamond__should_keep_using_unchanged_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ModelGenericParentWithInheritedField).field("bar").had(description="Bar"),
            schema(ModelGenericOtherParentWithInheritedField).field("value").had(description="Value"),
        )
    )

    diamond = schemas["2000-01-01"][ModelGenericDiamondWithInheritedField]
    assert diamond.model_validate({"value": InheritedItem(name="ok")}).value.name == "ok"


def test__schema_field_had__quoted_type_arg_field_of_other_parent_of_diamond__should_use_versioned_type_arg(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ModelGenericParentWithInheritedField).field("bar").had(description="Bar"),
            schema(QuotedGenericOtherParentWithInheritedField).field("value").had(description="Value"),
            schema(InheritedItem).field("name").had(name="old_name"),
        )
    )

    diamond = schemas["2000-01-01"][QuotedGenericDiamondWithInheritedField]
    assert diamond.model_validate({"value": {"old_name": "ok"}}).model_dump() == {
        "value": {"old_name": "ok"},
        "bar": 0,
    }


class GenericSubclassParent(BaseModel, Generic[_T]):
    value: _T
    bar: int = 0


class GenericSubclassChild(GenericSubclassParent[_T], Generic[_T]):
    pass


class GenericSubclassOtherChild(GenericSubclassParent[_T], Generic[_T]):
    pass


class GenericSubclassDiamond(GenericSubclassChild[_U], GenericSubclassOtherChild[_U], Generic[_U]):
    pass


def test__schema_field_had__field_of_other_parent_of_generic_diamond__should_keep_its_type_arg(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericSubclassParent).field("bar").had(description="Bar"),
            schema(GenericSubclassOtherChild).field("value").had(description="Value"),
        )
    )

    diamond = schemas["2000-01-01"][GenericSubclassDiamond[int]]
    assert diamond.model_fields["value"].annotation is int
    with pytest.raises(ValidationError):
        diamond.model_validate({"value": "abc"})


class ParentOfDiamondWithMixin(BaseModel):
    foo: str
    bar: int = 0


class MixinWithField:
    foo: int = 0


class LeftChildWithMixin(MixinWithField, ParentOfDiamondWithMixin):
    pass


class RightChildOfDiamondWithMixin(ParentOfDiamondWithMixin):
    pass


class DiamondWithMixinField(LeftChildWithMixin, RightChildOfDiamondWithMixin):
    pass


class ParentOfDiamondWithClassVar(BaseModel):
    foo: str
    bar: int = 0


class LeftChildWithClassVar(ParentOfDiamondWithClassVar):
    foo: ClassVar[str] = "hidden"  # ty: ignore[invalid-attribute-override]


class RightChildOfDiamondWithClassVar(ParentOfDiamondWithClassVar):
    pass


class DiamondWithClassVar(LeftChildWithClassVar, RightChildOfDiamondWithClassVar):
    pass


def test__schema_field_had__field_of_other_parent_of_diamond_whose_first_base_has_mixin__should_keep_mixin_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentOfDiamondWithMixin).field("bar").had(description="Bar"),
            schema(RightChildOfDiamondWithMixin).field("foo").had(description="Right"),
        )
    )

    diamond = schemas["2000-01-01"][DiamondWithMixinField]
    assert diamond.model_validate({}).model_dump() == {"foo": 0, "bar": 0}
    assert diamond.model_validate({"foo": 123}).model_dump() == {"foo": 123, "bar": 0}


def test__schema_field_had__field_of_other_parent_of_diamond_whose_first_base_hides_it__should_stay_hidden(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentOfDiamondWithClassVar).field("bar").had(description="Bar"),
            schema(LeftChildWithClassVar).field("bar").had(description="Left"),
            schema(RightChildOfDiamondWithClassVar).field("foo").had(description="Right"),
        )
    )

    diamond = schemas["2000-01-01"][DiamondWithClassVar]
    assert set(diamond.model_fields) == {"bar"}
    assert diamond.model_validate({}).model_dump() == {"bar": 0}


class ParentOfConstrainedDiamond(BaseModel):
    foo: int = 0
    bar: int = 0


class LeftChildOfConstrainedDiamond(ParentOfConstrainedDiamond):
    pass


class RightChildOfConstrainedDiamond(ParentOfConstrainedDiamond):
    pass


class ConstrainedDiamond(LeftChildOfConstrainedDiamond, RightChildOfConstrainedDiamond):
    pass


def test__schema_field_had__constraint_of_other_parent_of_diamond__should_apply_only_if_diamond_changes_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    right_instructions = (
        schema(ParentOfConstrainedDiamond).field("bar").had(description="Bar"),
        schema(RightChildOfConstrainedDiamond).field("foo").had(ge=0),
    )
    schemas = create_runtime_schemas(version_change(*right_instructions))

    # Pydantic takes the field of the diamond from its first base which does not have the constraint
    assert schemas["2000-01-01"][ConstrainedDiamond].model_validate({"foo": -1}).foo == -1
    with pytest.raises(ValidationError):
        schemas["2000-01-01"][RightChildOfConstrainedDiamond].model_validate({"foo": -1})
    assert schemas["2000-01-01"][LeftChildOfConstrainedDiamond].model_validate({"foo": -1}).foo == -1

    # The same instruction for the diamond before the one for its other parent applies to the diamond
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentOfConstrainedDiamond).field("bar").had(description="Bar"),
            schema(ConstrainedDiamond).field("foo").had(ge=0),
            schema(RightChildOfConstrainedDiamond).field("foo").had(ge=0),
        )
    )

    with pytest.raises(ValidationError):
        schemas["2000-01-01"][ConstrainedDiamond].model_validate({"foo": -1})
    assert schemas["2000-01-01"][ConstrainedDiamond].model_fields["foo"].description is None
    assert schemas["2000-01-01"][LeftChildOfConstrainedDiamond].model_validate({"foo": -1}).foo == -1

    # Once the diamond changes the field itself, it gets the changes of all of its parents
    schemas = create_runtime_schemas(
        version_change(*right_instructions, schema(ConstrainedDiamond).field("foo").had(description="Foo"))
    )

    with pytest.raises(ValidationError):
        schemas["2000-01-01"][ConstrainedDiamond].model_validate({"foo": -1})
    assert schemas["2000-01-01"][LeftChildOfConstrainedDiamond].model_validate({"foo": -1}).foo == -1


class ParentWithFieldThatGetsRenamed(BaseModel):
    foo: str = Field(default="", max_length=10)
    bar: int = 0


class ChildWithFieldThatGetsRenamed(ParentWithFieldThatGetsRenamed):
    pass


class GrandchildWithFieldThatGetsRenamed(ChildWithFieldThatGetsRenamed):
    pass


def _get_max_lengths(model: type[BaseModel]) -> dict[str, Union[int, None]]:
    return {
        name: next((m.max_length for m in field.metadata if hasattr(m, "max_length")), None)
        for name, field in model.model_fields.items()
        if name != "bar"
    }


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected"),
    [
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
            ],
            {
                ParentWithFieldThatGetsRenamed: {"old_foo": 10},
                ChildWithFieldThatGetsRenamed: {"old_foo": 3, "foo": 3},
                GrandchildWithFieldThatGetsRenamed: {"old_foo": 3, "foo": 3},
            },
            id="child_changed_field_then_parent_renamed_it",
        ),
        pytest.param(
            [
                [
                    schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3),
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                ]
            ],
            {
                ParentWithFieldThatGetsRenamed: {"old_foo": 10},
                ChildWithFieldThatGetsRenamed: {"old_foo": 3, "foo": 3},
            },
            id="child_changed_field_and_parent_renamed_it_in_one_version",
        ),
        pytest.param(
            [
                [schema(GrandchildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
            ],
            {
                ParentWithFieldThatGetsRenamed: {"foo": 10},
                ChildWithFieldThatGetsRenamed: {"foo": 10, "old_foo": 10},
                GrandchildWithFieldThatGetsRenamed: {"foo": 3, "old_foo": 3},
            },
            id="grandchild_changed_field_then_child_renamed_it",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
                [schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(name="older_foo")],
            ],
            {
                ParentWithFieldThatGetsRenamed: {"older_foo": 10},
                ChildWithFieldThatGetsRenamed: {"older_foo": 3, "foo": 3},
            },
            id="child_changed_field_then_parent_renamed_it_twice",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
                [schema(ParentWithFieldThatGetsRenamed).field("old_foo").didnt_exist],
            ],
            {ParentWithFieldThatGetsRenamed: {}, ChildWithFieldThatGetsRenamed: {"foo": 3}},
            id="child_changed_field_then_parent_renamed_and_deleted_it",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").didnt_exist],
            ],
            {ParentWithFieldThatGetsRenamed: {}, ChildWithFieldThatGetsRenamed: {"foo": 3}},
            id="child_changed_field_then_parent_deleted_it",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
                [schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(max_length=5)],
            ],
            {
                ParentWithFieldThatGetsRenamed: {"old_foo": 5},
                ChildWithFieldThatGetsRenamed: {"old_foo": 5, "foo": 5},
            },
            id="child_changed_field_then_parent_renamed_and_changed_it",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
                [schema(ChildWithFieldThatGetsRenamed).field("old_foo").had(max_length=5)],
            ],
            {
                ParentWithFieldThatGetsRenamed: {"old_foo": 10},
                ChildWithFieldThatGetsRenamed: {"old_foo": 5, "foo": 5},
            },
            id="child_changed_field_then_parent_renamed_it_then_child_changed_it_again",
        ),
    ],
)
def test__schema_field_had__parent_renamed_field_that_subclass_changed__should_keep_changes_of_subclass(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected: dict[type[BaseModel], dict[str, Union[int, None]]],
):
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest[1:])),
        version_change(
            schema(ParentWithFieldThatGetsRenamed).field("bar").had(description="Bar"), *instructions_from_newest[0]
        ),
    )

    assert {model: _get_max_lengths(schemas["2000-01-01"][model]) for model in expected} == expected


def test__schema_field_had__parent_renamed_field_and_changed_its_type__should_change_type_of_subclass(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(type=bytes)),
        version_change(schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")),
        version_change(
            schema(ParentWithFieldThatGetsRenamed).field("bar").had(description="Bar"),
            schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3),
        ),
    )

    child = schemas["2000-01-01"][ChildWithFieldThatGetsRenamed]
    assert child.model_fields["old_foo"].annotation is bytes
    assert _get_max_lengths(child)["old_foo"] == 3


def test__schema_field_had__generic_parent_renamed_field_that_subclass_changed__should_keep_its_concrete_type(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(GenericParentWithInheritedField).field("kind").had(name="old_kind")),
        version_change(
            schema(GenericParentWithInheritedField).field("bar").had(description="Bar"),
            schema(GenericChildWithInheritedField).field("kind").had(description="Child"),
        ),
    )

    child = schemas["2000-01-01"][GenericChildWithInheritedField]
    assert child.model_fields["old_kind"].annotation == Literal["a"]


class ParentWithRenameCollision(BaseModel):
    foo: str = Field(max_length=10)
    bar: int = 0


class ChildWithRenameCollision(ParentWithRenameCollision):
    pass


def test__schema_field_had__parent_renamed_another_field_to_name_of_renamed_field_and_deleted_it__should_drop_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildWithRenameCollision).field("foo").had(max_length=3),
            schema(ParentWithRenameCollision).field("foo").had(name="old"),
            schema(ParentWithRenameCollision).field("bar").had(name="old"),
            schema(ParentWithRenameCollision).field("old").didnt_exist,
        )
    )

    assert _get_max_lengths(schemas["2000-01-01"][ChildWithRenameCollision]) == {"foo": 3}


class ParentRenamedIntoFirstBase(BaseModel):
    foo: str = Field(max_length=10)


class FirstBaseWithField(BaseModel):
    old: int = 0


class FirstBaseWithClassVar(BaseModel):
    old: ClassVar[int] = 0


class GenericFirstBase(BaseModel, Generic[_T]):
    old: _T


class ChildOfFirstBaseWithField(FirstBaseWithField, ParentRenamedIntoFirstBase):
    pass


class ChildOfFirstBaseWithClassVar(FirstBaseWithClassVar, ParentRenamedIntoFirstBase):
    pass


class ChildOfGenericFirstBase(GenericFirstBase[int], ParentRenamedIntoFirstBase):
    pass


class MiddleWithField(ParentRenamedIntoFirstBase):
    old: int = 0


class ChildOfMiddleWithField(MiddleWithField):
    pass


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        (ChildOfFirstBaseWithField, {"old": 123, "foo": "abc"}),
        (ChildOfFirstBaseWithClassVar, {"foo": "abc"}),
        (ChildOfGenericFirstBase, {"old": 123, "foo": "abc"}),
        (ChildOfMiddleWithField, {"old": 123, "foo": "abc"}),
    ],
)
def test__schema_field_had__parent_renamed_field_to_name_that_first_base_has__should_keep_first_base_one(
    create_runtime_schemas: CreateRuntimeSchemas, child: type[BaseModel], expected: dict[str, Any]
):
    schemas = create_runtime_schemas(
        version_change(
            schema(child).field("foo").had(max_length=3),
            schema(ParentRenamedIntoFirstBase).field("foo").had(name="old"),
        )
    )

    assert schemas["2000-01-01"][child].model_validate({"foo": "abc", "old": 123}).model_dump() == expected


class ParentOfDiamondWithRenamedField(BaseModel):
    foo: str = Field(max_length=10)


class LeftChildOfDiamondWithRenamedField(ParentOfDiamondWithRenamedField):
    pass


class RightChildOfDiamondWithRenamedField(ParentOfDiamondWithRenamedField):
    pass


class DiamondWithRenamedField(LeftChildOfDiamondWithRenamedField, RightChildOfDiamondWithRenamedField):
    pass


def test__schema_field_had__parent_renamed_field_of_diamond_and_other_parent_changed_its_type__should_keep_type(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(DiamondWithRenamedField).field("foo").had(max_length=3),
            schema(ParentOfDiamondWithRenamedField).field("foo").had(name="old"),
            schema(RightChildOfDiamondWithRenamedField).field("old").had(type=bytes),
        )
    )

    diamond = schemas["2000-01-01"][DiamondWithRenamedField]
    assert diamond.model_validate({"foo": "abc", "old": "abc"}).model_dump() == {"foo": "abc", "old": "abc"}


class ParentRenamedIntoMiddleClassVar(BaseModel):
    foo: str = Field(max_length=10)


class MiddleWithVersionedClassVar(ParentRenamedIntoMiddleClassVar):
    pass


class ChildOfMiddleWithVersionedClassVar(MiddleWithVersionedClassVar):
    pass


def test__schema_field_had__parent_renamed_field_to_name_of_classvar_that_existed_in_middle__should_keep_classvar(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildOfMiddleWithVersionedClassVar).field("foo").had(max_length=3),
            schema(MiddleWithVersionedClassVar).field("old").existed_as(type=ClassVar[int], info=Field(default=7)),
            schema(ParentRenamedIntoMiddleClassVar).field("foo").had(name="old"),
        )
    )

    child = schemas["2000-01-01"][ChildOfMiddleWithVersionedClassVar]
    assert set(child.model_fields) == {"foo"}
    assert child.model_validate({"foo": "abc"}).model_dump() == {"foo": "abc"}


class ParentRenamedIntoMiddleProperty(BaseModel):
    foo: str = Field(max_length=10)


class MiddleWithProperty(ParentRenamedIntoMiddleProperty):
    @property
    def old(self) -> str:
        return self.foo  # pragma: no cover


class ChildOfMiddleWithProperty(MiddleWithProperty):
    pass


def test__schema_field_had__parent_renamed_field_to_name_of_property_of_middle__should_not_change_renamed_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildOfMiddleWithProperty).field("foo").had(max_length=3),
            schema(ParentRenamedIntoMiddleProperty).field("foo").had(name="old"),
        )
    )

    child = schemas["2000-01-01"][ChildOfMiddleWithProperty]
    assert not child.model_fields["old"].is_required()
    assert child.model_validate({"foo": "abc", "old": "abcd"}).model_dump() == {"foo": "abc", "old": "abcd"}


class CountingFactory:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1
        return self.value


class ParentWithDefaultFactory(BaseModel):
    foo: int = Field(default_factory=CountingFactory())
    bar: int = 0


class ChildWithDefaultFactory(ParentWithDefaultFactory):
    pass


def test__schema_field_had__inherited_field_with_stateful_default_factory__should_not_share_it_with_parent(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithDefaultFactory).field("bar").had(description="Bar"),
            schema(ChildWithDefaultFactory).field("foo").had(type=float),
        )
    )

    models = [schemas["2000-01-01"][model] for model in (ParentWithDefaultFactory, ChildWithDefaultFactory)]
    assert [model().foo for model in models + models] == [1, 1, 2, 2]


class MutableDefault:
    def __init__(self) -> None:
        self.items: list[str] = []


class ParentWithMutableDefault(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    foo: MutableDefault = MutableDefault()
    bar: int = 0


class ChildWithMutableDefault(ParentWithMutableDefault):
    pass


def test__schema_field_had__inherited_field_with_mutable_default__should_not_share_it_with_parent(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithMutableDefault).field("bar").had(description="Bar"),
            schema(ChildWithMutableDefault).field("foo").had(type=object),
        )
    )

    parent = schemas["2000-01-01"][ParentWithMutableDefault]()
    child = schemas["2000-01-01"][ChildWithMutableDefault]()
    parent.foo.items.append("parent")
    assert child.foo.items == []


class ParentWithDefaultFactoryToRename(BaseModel):
    foo: int = Field(default_factory=CountingFactory())


class ChildWithDefaultFactoryToRename(ParentWithDefaultFactoryToRename):
    pass


def test__schema_field_had__parent_renamed_field_with_default_factory__should_not_share_it_between_child_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildWithDefaultFactoryToRename).field("foo").had(type=float),
            schema(ParentWithDefaultFactoryToRename).field("foo").had(name="old_foo"),
        )
    )

    assert schemas["2000-01-01"][ChildWithDefaultFactoryToRename]().model_dump() == {"old_foo": 1, "foo": 1}


shared_counting_factory = CountingFactory()


class ParentWithFieldsThatShareDefaultFactoryToRename(BaseModel):
    foo: int = Field(default_factory=shared_counting_factory)
    bar: int = Field(default_factory=shared_counting_factory)
    baz: int = Field(default_factory=shared_counting_factory)


class ChildWithFieldsThatShareDefaultFactoryToRename(ParentWithFieldsThatShareDefaultFactoryToRename):
    pass


def test__schema_field_had__parent_renamed_fields_that_share_default_factory__should_share_it_with_parent_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildWithFieldsThatShareDefaultFactoryToRename).field("foo").had(type=float),
            schema(ChildWithFieldsThatShareDefaultFactoryToRename).field("bar").had(type=float),
            schema(ParentWithFieldsThatShareDefaultFactoryToRename).field("foo").had(name="old_foo"),
            schema(ParentWithFieldsThatShareDefaultFactoryToRename).field("bar").had(name="old_bar"),
        )
    )

    # Like the fields that the child inherits from the parent, the renamed ones share the parent's copy of the factory
    assert schemas["2000-01-01"][ChildWithFieldsThatShareDefaultFactoryToRename]().model_dump() == {
        "baz": 1,
        "old_foo": 2,
        "old_bar": 3,
        "foo": 1,
        "bar": 2,
    }


class ParentWithMutableDefaultToRename(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    foo: MutableDefault = MutableDefault()
    bar: int = 0


class ChildWithMutableDefaultToRename(ParentWithMutableDefaultToRename):
    pass


def test__schema_field_had__parent_renamed_field_with_mutable_default__should_not_share_it_between_child_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithMutableDefaultToRename).field("bar").had(description="Bar"),
            schema(ChildWithMutableDefaultToRename).field("foo").had(type=object),
            schema(ParentWithMutableDefaultToRename).field("foo").had(name="old_foo"),
        )
    )

    child = schemas["2000-01-01"][ChildWithMutableDefaultToRename]()
    child.foo.items.append("foo")
    assert child.old_foo.items == []  # ty: ignore[unresolved-attribute]


class ParentWithDefaults(BaseModel):
    with_factory: str = Field(default_factory=str)
    with_default: str = "default"
    bar: int = 0


class ChildWithDefaults(ParentWithDefaults):
    pass


class SiblingWithDefaults(ParentWithDefaults):
    pass


def test__schema_field_had__default_of_child_after_sibling_deleted_default_factory__should_replace_factory(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithDefaults).field("bar").had(description="Bar"),
            schema(SiblingWithDefaults).field("with_factory").didnt_have("default_factory"),
            schema(ChildWithDefaults).field("with_factory").had(default="old"),
        )
    )

    assert schemas["2000-01-01"][ChildWithDefaults]().with_factory == "old"
    assert schemas["2000-01-01"][ParentWithDefaults]().with_factory == ""
    assert schemas["2000-01-01"][SiblingWithDefaults].model_fields["with_factory"].is_required()


def test__schema_field_had__none_default_of_child_after_sibling_unset_default_factory__should_replace_factory(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithDefaults).field("bar").had(description="Bar"),
            schema(SiblingWithDefaults).field("with_factory").had(default_factory=None),  # ty: ignore[invalid-argument-type]
            schema(ChildWithDefaults).field("with_factory").had(default=None),
        )
    )

    assert schemas["2000-01-01"][ChildWithDefaults]().with_factory is None


@pytest.mark.parametrize(
    ("field_name", "attributes"),
    [
        ("with_factory", {"default": "old"}),
        ("with_default", {"default_factory": str}),
    ],
)
def test__schema_field_had__both_default_and_default_factory__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, field_name: str, attributes: dict[str, Any]
):
    # Cadwyn 7.4.0 failed with pydantic's TypeError here
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            f'Instructions leave the field "{field_name}" with both a default and a default_factory, which pydantic '
            'does not allow. The last instruction that changed one of them is for "ChildWithDefaults".'
        ),
    ):
        create_runtime_schemas(
            version_change(
                schema(ParentWithDefaults).field("bar").had(description="Bar"),
                schema(ChildWithDefaults).field(field_name).had(**attributes),
            )
        )


class ModelWithExplicitDefaultSentinels(BaseModel):
    with_default_and_none_factory: int = Field(default=1, default_factory=None)
    with_default: int = 1
    with_factory: int = Field(default_factory=int)


@pytest.mark.parametrize(
    ("field_name", "attributes", "expected"),
    [
        ("with_default_and_none_factory", {"description": "Old"}, 1),
        ("with_default", {"default": 2, "default_factory": None}, 2),
        ("with_default", {"default": ..., "default_factory": int}, 0),
        ("with_factory", {"default": PydanticUndefined}, 0),
    ],
)
def test__schema_field_had__default_or_default_factory_that_pydantic_treats_as_unset__should_not_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, field_name: str, attributes: dict[str, Any], expected: int
):
    schemas = create_runtime_schemas(
        version_change(schema(ModelWithExplicitDefaultSentinels).field(field_name).had(**attributes))
    )

    assert getattr(schemas["2000-01-01"][ModelWithExplicitDefaultSentinels](), field_name) == expected


class AmbiguousTruthValue:
    def __bool__(self) -> bool:
        raise ValueError("The truth value is ambiguous")  # pragma: no cover


class ElementwiseEqualDefault:
    """Like numpy arrays: comparing it gives an object whose truth value is ambiguous"""

    @override
    def __eq__(self, other: object) -> Any:
        return AmbiguousTruthValue()  # pragma: no cover

    __hash__ = object.__hash__


elementwise_equal_default = ElementwiseEqualDefault()


class ParentWithElementwiseEqualDefault(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    foo: ElementwiseEqualDefault = elementwise_equal_default
    bar: int = 0


class ChildWithElementwiseEqualDefault(ParentWithElementwiseEqualDefault):
    pass


class SiblingWithElementwiseEqualDefault(ParentWithElementwiseEqualDefault):
    pass


def test__schema_field_had__other_field_of_model_with_elementwise_equal_default__should_keep_default(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithElementwiseEqualDefault).field("bar").had(description="Bar"))
    )

    assert isinstance(schemas["2000-01-01"][ParentWithElementwiseEqualDefault]().foo, ElementwiseEqualDefault)


def test__schema_field_had__default_of_field_with_elementwise_equal_default_that_sibling_deleted__should_set_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithElementwiseEqualDefault).field("bar").had(description="Bar"),
            schema(SiblingWithElementwiseEqualDefault).field("foo").didnt_have("default"),
            schema(ChildWithElementwiseEqualDefault).field("foo").had(default=None),
        )
    )

    assert schemas["2000-01-01"][ChildWithElementwiseEqualDefault]().foo is None


def _factory() -> str:
    return "factory"


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected"),
    [
        pytest.param(
            [
                [
                    schema(ParentWithDefaults).field("with_default").had(default_factory=_factory),
                    schema(ParentWithDefaults).field("with_default").didnt_have("default_factory"),
                ],
                [schema(ChildWithDefaults).field("with_default").had(description="Child")],
            ],
            {ParentWithDefaults: "default", ChildWithDefaults: "default", SiblingWithDefaults: "default"},
            id="parent_added_and_deleted_default_factory_then_child_changed_something_else",
        ),
        pytest.param(
            [
                [
                    schema(SiblingWithDefaults).field("with_default").had(description="Sibling"),
                    schema(ChildWithDefaults).field("with_default").had(default_factory=_factory),
                    schema(ParentWithDefaults).field("with_default").had(default="parent"),
                    schema(ChildWithDefaults).field("with_default").didnt_have("default"),
                ]
            ],
            {ParentWithDefaults: "parent", ChildWithDefaults: "factory", SiblingWithDefaults: "parent"},
            id="child_added_default_factory_then_parent_changed_default_then_child_deleted_default",
        ),
    ],
)
def test__schema_field_had__default_and_default_factory_of_model__should_not_depend_on_other_models(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected: dict[type[ParentWithDefaults], str],
):
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest[1:])),
        version_change(schema(ParentWithDefaults).field("bar").had(description="Bar"), *instructions_from_newest[0]),
    )

    assert {model: schemas["2000-01-01"][model]().with_default for model in expected} == expected


class AnnotatedParentWithDefault(BaseModel):
    foo: Annotated[str, Field(description="Head")] = "default"
    bar: int = 0


class AnnotatedChildWithDefault(AnnotatedParentWithDefault):
    pass


class AnnotatedSiblingWithDefault(AnnotatedParentWithDefault):
    pass


def test__schema_field_had__default_factory_of_annotated_field_that_sibling_deleted__should_replace_default(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(AnnotatedParentWithDefault).field("bar").had(description="Bar"),
            schema(AnnotatedParentWithDefault).field("foo").had(default_factory=_factory),
            schema(AnnotatedSiblingWithDefault).field("foo").didnt_have("default_factory"),
        )
    )

    assert schemas["2000-01-01"][AnnotatedParentWithDefault]().foo == "factory"
    assert schemas["2000-01-01"][AnnotatedChildWithDefault]().foo == "factory"
    assert schemas["2000-01-01"][AnnotatedSiblingWithDefault]().foo == "default"


def _twenty() -> int:
    return 20


class AnnotatedParentWithDefaultInField(BaseModel):
    foo: Annotated[int, Field(default=0)]
    bar: int = 0


class AnnotatedChildWithDefaultInField(AnnotatedParentWithDefaultInField):
    pass


def test__schema_field_didnt_have__annotated_default_after_child_set_default_factory_to_none__should_keep_it_in_child(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(AnnotatedParentWithDefaultInField).field("bar").had(description="Bar"),
            schema(AnnotatedChildWithDefaultInField).field("foo").had(default_factory=None),  # ty: ignore[invalid-argument-type]
            schema(AnnotatedParentWithDefaultInField).field("foo").didnt_have("default"),
        )
    )

    # Cadwyn 7.4.0 ignored didnt_have("default") for the default from Field() in the annotation. Applying it would
    # make the older versions reject the requests without the field that they have always accepted, so it stays ignored
    assert schemas["2000-01-01"][AnnotatedParentWithDefaultInField]().foo == 0
    assert schemas["2000-01-01"][AnnotatedChildWithDefaultInField]().foo == 0


def test__schema_field_didnt_have__annotated_default_after_child_set_default_factory__should_use_factory(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(AnnotatedParentWithDefaultInField).field("bar").had(description="Bar"),
            schema(AnnotatedChildWithDefaultInField).field("foo").had(default_factory=_twenty),
            schema(AnnotatedParentWithDefaultInField).field("foo").didnt_have("default"),
        )
    )

    # Cadwyn 7.4.0 ignored didnt_have("default") for the default from Field() in the annotation. Applying it would
    # make the older versions reject the requests without the field that they have always accepted, so it stays ignored
    assert schemas["2000-01-01"][AnnotatedParentWithDefaultInField]().foo == 0
    assert schemas["2000-01-01"][AnnotatedChildWithDefaultInField]().foo == 20


class AnnotatedParentWithDefaultToReplaceWithFactory(BaseModel):
    foo: Annotated[Union[int, None], Field(default=1)]
    bar: int = 0


class AnnotatedChildWithDefaultToReplaceWithFactory(AnnotatedParentWithDefaultToReplaceWithFactory):
    pass


@pytest.mark.parametrize(
    "child_instruction",
    [
        schema(AnnotatedChildWithDefaultToReplaceWithFactory).field("foo").had(default_factory=None),  # ty: ignore[invalid-argument-type]
        schema(AnnotatedChildWithDefaultToReplaceWithFactory).field("foo").didnt_have("default_factory"),
    ],
)
def test__schema_field_had__ellipsis_default_with_factory_of_annotated_field_with_default__should_use_factory(
    create_runtime_schemas: CreateRuntimeSchemas, child_instruction: Any
):
    schemas = create_runtime_schemas(
        version_change(
            schema(AnnotatedParentWithDefaultToReplaceWithFactory).field("bar").had(description="Bar"),
            schema(AnnotatedParentWithDefaultToReplaceWithFactory)
            .field("foo")
            .had(default=..., default_factory=_twenty),
            child_instruction,
        )
    )

    # The child's instruction does not apply to the parent
    assert schemas["2000-01-01"][AnnotatedParentWithDefaultToReplaceWithFactory]().foo == 20
    assert schemas["2000-01-01"][AnnotatedChildWithDefaultToReplaceWithFactory]().foo == 1


class AnnotatedParentWithDefaultFactory(BaseModel):
    foo: Annotated[int, Field(default_factory=int)]
    bar: int = 0


class AnnotatedChildWithDefaultFactory(AnnotatedParentWithDefaultFactory):
    pass


class AnnotatedSiblingWithDefaultFactory(AnnotatedParentWithDefaultFactory):
    pass


def test__schema_field_had__default_of_child_after_parent_deleted_annotated_default_factory__should_use_default(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            # The child gets its own annotation of the field with the factory before the parent deletes it
            schema(AnnotatedChildWithDefaultFactory).field("foo").had(description="Child"),
            schema(AnnotatedParentWithDefaultFactory).field("foo").didnt_have("default_factory"),
            schema(AnnotatedSiblingWithDefaultFactory).field("foo").had(default_factory=None),  # ty: ignore[invalid-argument-type]
            schema(AnnotatedChildWithDefaultFactory).field("foo").had(default=5),
        )
    )

    assert schemas["2000-01-01"][AnnotatedChildWithDefaultFactory]().foo == 5


def test__schema_field_had__annotated_default_factory_with_assigned_default__should_raise_pydantic_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    # pydantic<2.12 accepts the model itself but not the field that Cadwyn copies from it
    with pytest.raises(TypeError, match="cannot specify both default and default_factory"):

        class ModelWithAnnotatedFactoryAndAssignedDefault(BaseModel):
            foo: Annotated[int, Field(default_factory=int)] = 5
            bar: int = 0

        create_runtime_schemas(  # pragma: no cover # Pydantic 2.11
            version_change(schema(ModelWithAnnotatedFactoryAndAssignedDefault).field("bar").had(description="Bar"))
        )


class ParentWithoutField(BaseModel):
    pass


class LeftChildWithoutField(ParentWithoutField):
    pass


class RightChildWithoutField(ParentWithoutField):
    pass


class DiamondWithoutField(LeftChildWithoutField, RightChildWithoutField):
    pass


class ParentWithFieldToRename(BaseModel):
    foo: str = Field(default="", max_length=10)


class LeftChildWithFieldToRename(ParentWithFieldToRename):
    pass


class RightChildWithFieldToRename(ParentWithFieldToRename):
    pass


class DiamondWithFieldToRename(LeftChildWithFieldToRename, RightChildWithFieldToRename):
    pass


@pytest.mark.parametrize(
    ("newer_instruction", "older_instruction", "diamond", "field_name"),
    [
        pytest.param(
            schema(ParentWithoutField).field("foo").existed_as(type=str, info=Field(default="", max_length=10)),
            schema(RightChildWithoutField).field("foo").had(max_length=3),
            DiamondWithoutField,
            "foo",
            id="field_that_existed_as",
        ),
        pytest.param(
            schema(ParentWithFieldToRename).field("foo").had(name="old_foo"),
            schema(RightChildWithFieldToRename).field("old_foo").had(max_length=3),
            DiamondWithFieldToRename,
            "old_foo",
            id="renamed_field",
        ),
    ],
)
def test__schema_field_had__field_of_other_parent_of_diamond_that_only_older_versions_have__should_keep_first(
    create_runtime_schemas: CreateRuntimeSchemas,
    newer_instruction: Any,
    older_instruction: Any,
    diamond: type[BaseModel],
    field_name: str,
):
    schemas = create_runtime_schemas(version_change(older_instruction), version_change(newer_instruction))

    field = schemas["2000-01-01"][diamond].model_fields[field_name]
    # Pydantic takes the field of the diamond from its first base which does not have the change
    assert [m.max_length for m in field.metadata if hasattr(m, "max_length")] == [10]


#######
# HAD #
#######


def assert_field_had_changes_apply(
    model: type[BaseModel],
    attr: str,
    attr_value: Any,
    create_runtime_schemas: CreateRuntimeSchemas,
) -> dict[str, SchemaGenerator]:
    schemas = create_runtime_schemas(version_change(schema(model).field("foo").had(**{attr: attr_value})))

    field_info = schemas["2000-01-01"][model].model_fields["foo"]
    if attr in FieldInfo.metadata_lookup:
        # We do this because _PydanticGeneralMetadata does not have a proper `__eq__`
        assert repr(FieldInfo._collect_metadata({attr: attr_value})[0]) in [repr(obj) for obj in field_info.metadata]
    else:
        assert getattr(field_info, attr) == attr_value

    return schemas


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("default", 100),
        ("alias", "myalias"),
        ("title", "mytitle"),
        ("description", "mydescription"),
        ("gt", 3),
        ("ge", 4),
        ("lt", 5),
        ("le", 6),
        ("multiple_of", 7),
        ("repr", False),
    ],
)
def test__schema_field_had__modifying_int_field(
    attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas
):
    """Guarantee that we can handle all parameter types we provide"""
    assert_field_had_changes_apply(
        SchemaWithOneIntField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("min_length", 20),
        ("max_length", 50),
    ],
)
def test__schema_field_had__str_field(attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas):
    assert_field_had_changes_apply(
        SchemaWithOneStrField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


def test__schema_field_had__pattern(create_runtime_schemas: CreateRuntimeSchemas):
    assert_field_had_changes_apply(
        SchemaWithOneStrField,
        "pattern",
        r"hewwo darkness",
        create_runtime_schemas,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("max_digits", 12),
        ("decimal_places", 15),
    ],
)
def test__schema_field_had__decimal_field(attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas):
    from decimal import Decimal

    class SchemaWithOneDecimalField(BaseModel):
        foo: Decimal

    assert_field_had_changes_apply(
        SchemaWithOneDecimalField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [("exclude", True)],
)
def test__schema_field_had__list_of_int_field(attr: str, attr_value: Any, create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithOneListOfIntField(BaseModel):
        foo: list[int]

    assert_field_had_changes_apply(
        SchemaWithOneListOfIntField,
        attr,
        attr_value,
        create_runtime_schemas,
    )


def test__schema_field_had__float_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithOneFloatField(BaseModel):
        foo: float

    assert_field_had_changes_apply(
        SchemaWithOneFloatField,
        "allow_inf_nan",
        attr_value=False,
        create_runtime_schemas=create_runtime_schemas,
    )


def test_schema_field_had__json_schema_extra_as_dict(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithFooHadDictJsonSchemaExtra(BaseModel):
        foo: str

    schemas = assert_field_had_changes_apply(
        SchemaWithFooHadDictJsonSchemaExtra,
        "json_schema_extra",
        {"example": "bar"},
        create_runtime_schemas,
    )

    # Validate the JSON Schema produced by Pydantic contains the extra
    model_cls = schemas["2000-01-01"][SchemaWithFooHadDictJsonSchemaExtra]
    json_schema = model_cls.model_json_schema()
    assert json_schema["properties"]["foo"]["example"] == "bar"


def test__schema_field_had__json_schema_extra_as_callable(create_runtime_schemas: CreateRuntimeSchemas):
    def modify_schema(schema_dict: dict[str, Any]):
        schema_dict["example"] = "bar"
        schema_dict["custom"] = 42

    class SchemaWithFooHadCallableJsonSchemaExtra(BaseModel):
        foo: str

    schemas = assert_field_had_changes_apply(
        SchemaWithFooHadCallableJsonSchemaExtra,
        "json_schema_extra",
        modify_schema,
        create_runtime_schemas,
    )

    # Validate the JSON Schema produced by Pydantic contains changes from the callable
    model_cls = schemas["2000-01-01"][SchemaWithFooHadCallableJsonSchemaExtra]
    json_schema = model_cls.model_json_schema()
    props = json_schema["properties"]["foo"]
    assert props["example"] == "bar"
    assert props["custom"] == 42


def test__schema_field_didnt_have__removing_default(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithDefaults(BaseModel):
        foo: str = "hewwo"
        bar: int = Field(default=83)

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithDefaults).field("foo").didnt_have("default"),
            schema(SchemaWithDefaults).field("bar").didnt_have("default"),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: str
        bar: int

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithDefaults], ExpectedSchema)


class SchemaWithConstraints(BaseModel):
    foo: conint(lt=7)  # ty: ignore[invalid-type-form]
    bar: str = Field(min_length=0, max_length=7)


def test__schema_field_didnt_have__constrained_field_constraints_removed__constraints_do_not_render(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithConstraints).field("foo").didnt_have("lt"),
            schema(SchemaWithConstraints).field("bar").didnt_have("max_length", "min_length"),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: conint()  # ty: ignore[invalid-type-form]
        bar: str

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithConstraints], ExpectedSchema)


def test__schema_field_had_constrained_field__only_non_constraint_field_args_were_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithConstraints).field("foo").had(alias="foo1"),
            schema(SchemaWithConstraints).field("bar").had(alias="bar1"),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: conint(lt=7) = Field(alias="foo1")  # ty: ignore[invalid-type-form]
        bar: str = Field(min_length=0, max_length=7, alias="bar1")

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithConstraints], ExpectedSchema)


def test__schema_field_had_constrained_field__field_is_an_unconstrained_union(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Schema(BaseModel):
        foo: Union[int, None] = Field(default=None)

    schemas = create_runtime_schemas(version_change(schema(Schema).field("foo").had(ge=0)))

    class ExpectedSchema(BaseModel):
        foo: Union[int, None] = Field(default=None, ge=0)

    assert_models_are_equal(schemas["2000-01-01"][Schema], ExpectedSchema)


def test__schema_field_had_constrained_field__constraints_have_been_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithConstraints).field("foo").had(gt=8),
            schema(SchemaWithConstraints).field("bar").had(min_length=2),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: conint(lt=7) = Field(gt=8)  # ty: ignore[invalid-type-form]
        bar: str = Field(min_length=2, max_length=7)

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithConstraints], ExpectedSchema)


def test__schema_field_had_constrained_field__both_constraints_and_non_constraints_have_been_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithConstraints).field("foo").had(gt=8, alias="foo1"),
            schema(SchemaWithConstraints).field("bar").had(min_length=2, alias="bar1"),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: conint(lt=7) = Field(alias="foo1", gt=8)  # ty: ignore[invalid-type-form]
        bar: str = Field(min_length=2, max_length=7, alias="bar1")

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithConstraints], ExpectedSchema)


class SchemaWithConstraintsAndField(BaseModel):
    foo: constr(max_length=5) = Field(default="hewwo")  # ty: ignore[invalid-type-form]


def test__schema_field_had_constrained_field__constraint_field_args_were_modified_in_type(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithConstraintsAndField).field("foo").had(type=constr(max_length=6123123121)),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[str, StringConstraints(max_length=6123123121)] = Field(default="hewwo")

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithConstraintsAndField], ExpectedSchema)


def test__schema_field_had_constrained_field__constraint_only_args_were_modified_in_type(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithConstraintsAndField).field("foo").had(type=constr(max_length=6, strip_whitespace=True)),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=6)] = Field(default="hewwo")

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithConstraintsAndField], ExpectedSchema)


class SchemaWithAnnotatedConstraints(BaseModel):
    foo: Annotated[conint(lt=7), Field(description="awaw")]  # ty: ignore[invalid-type-form]


def test__schema_field_didnt_have_annotated_constrained_field(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithAnnotatedConstraints).field("foo").didnt_have("lt"))
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[conint(), Field(description="awaw")]  # ty: ignore[invalid-type-form]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithAnnotatedConstraints], ExpectedSchema)


class FieldInfoWithExtraAttribute(FieldInfo):  # ty: ignore[subclass-of-final-class]
    __slots__ = ("extra",)

    def __init__(self, *, extra: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.extra = extra


class SchemaWithFieldInfoSubclassInAnnotated(BaseModel):
    foo: Annotated[str, FieldInfoWithExtraAttribute(extra="hello", min_length=1, description="New description")]
    bar: Annotated[str, FieldInfoWithExtraAttribute(extra="hello", min_length=1, description="New description")]


def test__schema_field_had_and_didnt_have__field_info_subclass_in_annotated__should_keep_its_type_and_attributes(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithFieldInfoSubclassInAnnotated).field("foo").had(min_length=5, description="Old"),
            schema(SchemaWithFieldInfoSubclassInAnnotated).field("bar").didnt_have("min_length", "description"),
        )
    )
    old_schema = schemas["2000-01-01"][SchemaWithFieldInfoSubclassInAnnotated]

    for field_name in ("foo", "bar"):
        field_infos = get_type_hints(old_schema, include_extras=True)[field_name].__metadata__
        assert [(type(field_info), field_info.extra) for field_info in field_infos] == [
            (FieldInfoWithExtraAttribute, "hello")
        ]
    # Cadwyn 7.4.0 ignored had() for a constraint that the annotation's metadata sets (since pydantic 2.12). Applying
    # it would make the older versions reject the requests and responses that they have always accepted, so it stays
    # ignored
    assert old_schema.model_json_schema()["properties"] == {
        "foo": {
            "description": "Old",
            "minLength": 5 if HAD_CHANGES_CONSTRAINTS_OF_FIELD_IN_ANNOTATED else 1,
            "title": "Foo",
            "type": "string",
        },
        # Cadwyn 7.4.0 also ignored didnt_have() for the constraints of a Field() in the annotation's metadata. Removing
        # them could make the older versions reject or change the data that they have always accepted or returned, so
        # it stays ignored. Only the attributes that document the field get removed
        "bar": {"minLength": 1, "title": "Bar", "type": "string"},
    }


class TaggedCat(BaseModel):
    kind: Literal["cat"] = "cat"


class TaggedDog(BaseModel):
    kind: Literal["dog"] = "dog"


def _title_of_field(name: str, field_info: FieldInfo) -> str:
    return name.upper()


# The type and the Field() arguments that set every attribute that field().didnt_have() accepts
ANNOTATED_FIELDS_WITH_EVERY_ATTRIBUTE: dict[str, tuple[Any, dict[str, Any]]] = {
    "default": (int, {"default": 1}),
    "alias": (str, {"alias": "a"}),
    "alias_priority": (str, {"alias": "a", "alias_priority": 1}),
    "default_factory": (list[int], {"default_factory": list}),
    "validation_alias": (str, {"validation_alias": "v"}),
    "serialization_alias": (str, {"serialization_alias": "s"}),
    "title": (str, {"title": "Title"}),
    "field_title_generator": (str, {"field_title_generator": _title_of_field}),
    "description": (str, {"description": "Description"}),
    "examples": (str, {"examples": ["example"]}),
    "exclude": (str, {"exclude": True}),
    "deprecated": (str, {"deprecated": "Deprecated"}),
    "frozen": (str, {"frozen": True}),
    "validate_default": (str, {"validate_default": True}),
    "repr": (str, {"repr": False}),
    "init": (str, {"init": False}),
    "init_var": (str, {"init_var": True}),
    "kw_only": (str, {"kw_only": True}),
    "fail_fast": (list[int], {"fail_fast": True}),
    "gt": (int, {"gt": 1}),
    "ge": (int, {"ge": 1}),
    "lt": (int, {"lt": 1}),
    "le": (int, {"le": 1}),
    "strict": (int, {"strict": True}),
    "coerce_numbers_to_str": (str, {"coerce_numbers_to_str": True}),
    "multiple_of": (int, {"multiple_of": 2}),
    "allow_inf_nan": (float, {"allow_inf_nan": False}),
    "max_digits": (Decimal, {"max_digits": 3}),
    "decimal_places": (Decimal, {"decimal_places": 2}),
    "min_length": (str, {"min_length": 1}),
    "max_length": (str, {"max_length": 1}),
    "union_mode": (Union[int, str], {"union_mode": "left_to_right"}),
    "pattern": (str, {"pattern": "^a"}),
    "discriminator": (Union[TaggedCat, TaggedDog], {"discriminator": "kind"}),
    "json_schema_extra": (str, {"json_schema_extra": {"key": "value"}}),
}
# Only pydantic v1 had them, and pydantic v2 rejects them in Field()
FIELD_ATTRIBUTES_OF_PYDANTIC_V1 = {"const", "allow_mutation"}
# Cadwyn 7.4.0 ignored didnt_have() for all attributes of a Field() in the annotation's metadata. Removing any
# attribute but the ones that only document the field could make the older versions reject or change the data that
# they have always accepted or returned, so only these get removed
# (json_schema_extra holds arbitrary data that validators can read, so it isn't one of them)
ATTRIBUTES_THAT_DIDNT_HAVE_REMOVES_FROM_FIELD_IN_ANNOTATED = {"title", "description", "examples", "deprecated"}


def _get_field_attribute(field_info: FieldInfo, attr_name: str) -> Any:
    if attr_name in FieldInfo.metadata_lookup:
        return collect_known_metadata(field_info.metadata)[0].get(attr_name)
    return getattr(field_info, attr_name)


def test__schema_field_didnt_have__every_attribute_of_field_in_annotated__should_be_in_the_table():
    assert set(ANNOTATED_FIELDS_WITH_EVERY_ATTRIBUTE) | FIELD_ATTRIBUTES_OF_PYDANTIC_V1 == set(
        get_args(PossibleFieldAttributes)
    )
    assert set(ANNOTATED_FIELDS_WITH_EVERY_ATTRIBUTE) >= ATTRIBUTES_THAT_DIDNT_HAVE_REMOVES_FROM_FIELD_IN_ANNOTATED


@pytest.mark.parametrize("attr_name", ANNOTATED_FIELDS_WITH_EVERY_ATTRIBUTE)
def test__schema_field_didnt_have__attribute_of_field_in_annotated__should_only_remove_documentation(
    create_runtime_schemas: CreateRuntimeSchemas,
    attr_name: Any,
):
    field_type, field_kwargs = ANNOTATED_FIELDS_WITH_EVERY_ATTRIBUTE[attr_name]

    class SchemaWithAttributeInAnnotated(BaseModel):
        foo: Annotated[field_type, Field(**field_kwargs)]

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithAttributeInAnnotated).field("foo").didnt_have(attr_name)),
    )
    head_field = SchemaWithAttributeInAnnotated.model_fields["foo"]
    old_field = schemas["2000-01-01"][SchemaWithAttributeInAnnotated].model_fields["foo"]

    head_value = _get_field_attribute(head_field, attr_name)
    assert head_value not in (None, PydanticUndefined)
    if attr_name in ATTRIBUTES_THAT_DIDNT_HAVE_REMOVES_FROM_FIELD_IN_ANNOTATED:
        assert _get_field_attribute(old_field, attr_name) == _get_field_attribute(Field(), attr_name)
    else:
        assert _get_field_attribute(old_field, attr_name) == head_value


def test__schema_field_had_annotated_constrained_field(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithAnnotatedConstraints).field("foo").had(alias="foo1"))
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[conint(lt=7), Field(description="awaw", alias="foo1")]  # ty: ignore[invalid-type-form]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithAnnotatedConstraints], ExpectedSchema)


def test__schema_field_had_annotated_constrained_field__adding_default_default_should_be_added_outside_of_annotation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithAnnotatedConstraints).field("foo").had(default=2)),
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[conint(lt=7), Field(description="awaw")] = 2  # ty: ignore[invalid-type-form]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithAnnotatedConstraints], ExpectedSchema)


def test__schema_field_had_annotated_constrained_field__adding_one_other_constraint(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(SchemaWithAnnotatedConstraints).field("foo").had(gt=8)))

    class ExpectedSchema(BaseModel):
        foo: Annotated[conint(lt=7), Field(description="awaw", gt=8)]  # ty: ignore[invalid-type-form]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithAnnotatedConstraints], ExpectedSchema)


def test__schema_field_had_annotated_constrained_field__adding_another_constraint_and_an_attribute(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithAnnotatedConstraints).field("foo").had(gt=8, alias="foo1"))
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[conint(lt=7), Field(description="awaw", alias="foo1", gt=8)]  # ty: ignore[invalid-type-form]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithAnnotatedConstraints], ExpectedSchema)


def test__schema_field_had_constrained_field__schema_has_special_constraints_constraints_have_been_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithSpecialConstraints(BaseModel):
        foo: constr(to_upper=True)  # ty: ignore[invalid-type-form]

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithSpecialConstraints).field("foo").had(max_length=8))
    )

    class ExpectedSchema(BaseModel):
        foo: constr(to_upper=True) = Field(max_length=8)  # ty: ignore[invalid-type-form]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithSpecialConstraints], ExpectedSchema)


def test__schema_field_had_constrained_field__schema_has_special_constraints_constraints_have_been_modified__pydantic2(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithSpecialConstraints(BaseModel):
        foo: Annotated[str, StringConstraints(to_upper=True)]

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithSpecialConstraints).field("foo").had(max_length=8))
    )

    class ExpectedSchema(BaseModel):
        foo: Annotated[str, StringConstraints(to_upper=True)] = Field(max_length=8)

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithSpecialConstraints], ExpectedSchema)

    assert schemas["2000-01-01"][SchemaWithSpecialConstraints](foo="hewwo").foo == "HEWWO"
    with pytest.raises(ValidationError):
        assert schemas["2000-01-01"][SchemaWithSpecialConstraints](foo="sdwdwewdwd").foo


def test__schema_field_had__default_factory(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(  # pragma: no branch
        version_change(schema(SchemaWithOneStrField).field("foo").had(default_factory=lambda: "mew"))
    )

    assert schemas["2000-01-01"][SchemaWithOneStrField]().foo == "mew"  # ty: ignore[missing-argument]


def test__schema_field_had__type(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").had(type=bytes)))

    class ExpectedSchema(BaseModel):
        foo: bytes

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)


def test__schema_field_had_name(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").had(name="doo")))

    class ExpectedSchema(BaseModel):
        doo: str

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithOneStrField], ExpectedSchema)


def my_default_factory():
    raise NotImplementedError


class ModelWithWeirdFields(BaseModel):
    foo: dict = Field(default={"a": "b"})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: Literal[MyEnum.baz]


def test__schema_field_had__with_pre_existing_weird_data_types(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(ModelWithWeirdFields).field("bad").existed_as(type=int)))

    class ExpectedSchema(BaseModel):
        foo: dict = Field(default={"a": "b"})
        bar: list[int] = Field(default_factory=my_default_factory)
        baz: Literal[MyEnum.baz]
        bad: int

    assert_models_are_equal(schemas["2000-01-01"][ModelWithWeirdFields], ExpectedSchema)


def test__schema_field_had__with_weird_data_types__with_all_fields_modified(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ModelWithWeirdFields).field("foo").had(description="..."),
            schema(ModelWithWeirdFields).field("bar").had(description="..."),
            schema(ModelWithWeirdFields).field("baz").had(description="..."),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: dict = Field(default={"a": "b"}, description="...")
        bar: list[int] = Field(default_factory=my_default_factory, description="...")
        baz: Literal[MyEnum.baz] = Field(description="...")

    assert_models_are_equal(schemas["2000-01-01"][ModelWithWeirdFields], ExpectedSchema)


def test__union_fields(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithUnionFields(BaseModel):
        foo: Union[int, str]
        bar: Union[EmptySchema, None]

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithUnionFields).field("baz").existed_as(type=Union[int, EmptySchema]),
            schema(SchemaWithUnionFields).field("daz").existed_as(type=Union[int, EmptySchema]),
        )
    )

    class ExpectedSchema(BaseModel):
        foo: Union[int, str]
        bar: Union[EmptySchema, None]
        baz: Union[int, EmptySchema]
        daz: Union[int, EmptySchema]

    assert_models_are_equal(schemas["2000-01-01"][SchemaWithUnionFields], ExpectedSchema)


def test__schema_that_overrides_fields_from_mro(create_runtime_schemas: CreateRuntimeSchemas):
    class ParentSchema(BaseModel):
        foo: int = Field(default=83)
        bar: int = Field(default=83)

    class SchemaThatOverridesField(ParentSchema):
        foo: str = Field(description="What?")  # ty: ignore[invalid-attribute-override]  # Test a deliberate field type override.
        bar: str = Field(description="What?")  # ty: ignore[invalid-attribute-override]  # Test a deliberate field type override.

    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaThatOverridesField).field("foo").had(type=bytes),
            schema(SchemaThatOverridesField).field("bar").had(alias="baz"),
        )
    )

    class ExpectedSchema(schemas["2000-01-01"][ParentSchema]):
        foo: bytes = Field(description="What?")
        bar: str = Field(description="What?", alias="baz")

    assert_models_are_equal(schemas["2000-01-01"][SchemaThatOverridesField], ExpectedSchema)


def test__schema_field_existed_as__already_existing_field__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a field "foo" to "SchemaWithOneStrField" in '
            '"MyVersionChange" but there is already a field with that name.',
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").existed_as(type=int)))


def test__schema_field_didnt_exist__field_is_missing__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a field "bar" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("bar").didnt_exist))


def test__schema_field_didnt_have__using_incorrect_attribute__should_raise_error():
    with pytest.raises(
        CadwynStructureError,
        match=re.escape("Unknown attribute 'defaults'. Are you sure it's a valid field attribute?"),
    ):
        version_change(schema(BaseModel).field("foo").didnt_have("defaults"))  # ty: ignore[invalid-argument-type]


def test__schema_field_didnt_have__removing_nonexistent_attribute__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete the attribute "description" of field "foo" from "SchemaWithOneStrField" '
            'in "MyVersionChange" but it already doesn\'t have that attribute.'
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").didnt_have("description")))


def test__schema_field_had_name__name_is_the_same_as_before__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of field "foo" from "SchemaWithOneStrField" '
            'in "MyVersionChange" but it already has that name.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrField).field("foo").had(name="foo")),
        )


def test__schema_field_had__change_to_the_same_field_type__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "foo" to "<class \'str\'>" from'
            ' "SchemaWithOneStrField" in "MyVersionChange" but it already has type "<class \'str\'>"',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrField).field("foo").had(type=str)),
        )


def test__schema_field_had__change_attr_to_same_value__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithOneStrField(BaseModel):
        foo: str = Field(default="wow")

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "default" of field "foo" from "SchemaWithOneStrField" to \'wow\' '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(SchemaWithOneStrField).field("foo").had(default="wow")),
        )


def test__schema_field_had__change_metadata_attr_to_same_value__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "gt" of field "foo" from "EmptySchema" to 8 '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(EmptySchema).field("foo").had(gt=8)),
            version_change(
                schema(EmptySchema).field("foo").existed_as(type=int, info=Field(gt=8, lt=12)),
            ),
        )


def test__schema_field_had__nonexistent_field__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the field "boo" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("boo").had(type=int)))


def test__schema_with_computed_field__should_be_recreated_in_older_version(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithComputedField(BaseModel):
        image_file_key: str = Field(exclude=True)

        @computed_field
        @property
        def image_url(self) -> str:
            return f"https://example.com/{self.image_file_key}"

    schemas = create_runtime_schemas(version_change())

    latest_model = schemas["2001-01-01"][SchemaWithComputedField]
    old_model = schemas["2000-01-01"][SchemaWithComputedField]

    latest_instance = latest_model(image_file_key="test.jpg")
    old_instance = old_model(image_file_key="test.jpg")

    assert latest_instance.image_url == "https://example.com/test.jpg"
    assert old_instance.image_url == "https://example.com/test.jpg"


def test__schema_with_computed_field__remove_computed_field(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithComputedField(BaseModel):
        image_file_key: str = Field(exclude=True)

        @computed_field
        @property
        def image_url(self) -> str:
            return f"https://example.com/{self.image_file_key}"

    schemas = create_runtime_schemas(version_change(schema(SchemaWithComputedField).field("image_url").didnt_exist))

    latest_model = schemas["2001-01-01"][SchemaWithComputedField]
    latest_instance = latest_model(image_file_key="test.jpg")
    assert latest_instance.image_url == "https://example.com/test.jpg"

    old_model = schemas["2000-01-01"][SchemaWithComputedField]
    old_instance = old_model(image_file_key="test.jpg")

    assert not hasattr(old_instance, "image_url")
    assert "image_url" not in old_instance.model_dump()


def test__schema_with_classvar__should_be_recreated_in_older_version(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithClassVar(BaseModel):
        regular_field: str
        class_level_config: ClassVar[str] = "default_config"

    schemas = create_runtime_schemas(version_change())

    latest_model = schemas["2001-01-01"][SchemaWithClassVar]
    old_model = schemas["2000-01-01"][SchemaWithClassVar]

    latest_instance = latest_model(regular_field="test")
    old_instance = old_model(regular_field="test")

    assert latest_model.class_level_config == "default_config"
    assert old_model.class_level_config == "default_config"

    assert "class_level_config" not in latest_instance.model_dump()
    assert "class_level_config" not in old_instance.model_dump()

    assert latest_instance.regular_field == "test"
    assert old_instance.regular_field == "test"

    assert "class_level_config" in latest_model.__annotations__
    assert "class_level_config" in old_model.__annotations__

    assert get_origin(latest_model.__annotations__["class_level_config"]) is ClassVar
    assert get_origin(old_model.__annotations__["class_level_config"]) is ClassVar


def test__schema_with_classvar__remove_classvar(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithClassVar(BaseModel):
        regular_field: str
        class_level_config: ClassVar[str] = "default_config"

    schemas = create_runtime_schemas(version_change(schema(SchemaWithClassVar).field("class_level_config").didnt_exist))

    latest_model = schemas["2001-01-01"][SchemaWithClassVar]
    latest_instance = latest_model(regular_field="test")

    assert latest_model.class_level_config == "default_config"
    assert "class_level_config" not in latest_instance.model_dump()

    old_model = schemas["2000-01-01"][SchemaWithClassVar]
    old_instance = old_model(regular_field="test")

    assert not hasattr(old_model, "class_level_config")
    assert "class_level_config" not in old_instance.model_dump()

    assert latest_instance.regular_field == "test"
    assert old_instance.regular_field == "test"


def test__schema_with_classvar__add_classvar_field(create_runtime_schemas: CreateRuntimeSchemas):
    class SchemaWithoutClassVar(BaseModel):
        regular_field: str

    schemas = create_runtime_schemas(
        version_change(),
        version_change(
            schema(SchemaWithoutClassVar)
            .field("new_config")
            .existed_as(type=ClassVar[str], info=Field(default="added_config")),
            schema(SchemaWithoutClassVar).field("new_config_without_value").existed_as(type=ClassVar[str]),
        ),
    )
    latest_model = schemas["2002-01-01"][SchemaWithoutClassVar]
    mid_model = schemas["2001-01-01"][SchemaWithoutClassVar]
    old_model = schemas["2000-01-01"][SchemaWithoutClassVar]

    assert not hasattr(latest_model, "new_config")
    assert mid_model.new_config == "added_config"  # ty: ignore[unresolved-attribute]
    assert old_model.new_config == "added_config"  # ty: ignore[unresolved-attribute]

    assert not hasattr(latest_model, "new_config_without_value")
    assert not hasattr(mid_model, "new_config_without_value")
    assert not hasattr(old_model, "new_config_without_value")
