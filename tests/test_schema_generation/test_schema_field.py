import collections
import contextlib
import copy
import dataclasses
import json
import re
import threading
import time
import typing
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from enum import Enum, auto
from typing import (
    Annotated,
    Any,
    ClassVar,
    Generic,
    Literal,
    NamedTuple,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import pytest
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    ValidationError,
    computed_field,
    conint,
    constr,
    create_model,
)
from pydantic._internal._known_annotated_metadata import collect_known_metadata
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import TypeAliasType, override

from cadwyn import Version, VersionBundle, VersionChange
from cadwyn.exceptions import (
    CadwynStructureError,
    InvalidGenerationInstructionError,
    SchemaGenerationError,
)
from cadwyn.schema_generation import SchemaGenerator, generate_versioned_models
from cadwyn.structure import enum, schema
from cadwyn.structure.schemas import PossibleFieldAttributes
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


class LeftChildWithMixin(MixinWithField, ParentOfDiamondWithMixin):  # ty: ignore[invalid-attribute-override]  # The mixin deliberately retypes `foo`.
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


_RENAME_FOO_IN_PARENT_AND_CHILD = [
    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
    schema(ChildWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
]


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected"),
    [
        pytest.param(
            [[schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)], _RENAME_FOO_IN_PARENT_AND_CHILD],
            {
                ParentWithFieldThatGetsRenamed: {"old_foo": 10},
                ChildWithFieldThatGetsRenamed: {"old_foo": 3},
                GrandchildWithFieldThatGetsRenamed: {"old_foo": 3},
            },
            id="child_changed_field_then_parent_and_child_renamed_it",
        ),
        pytest.param(
            [[schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3), *_RENAME_FOO_IN_PARENT_AND_CHILD]],
            {ParentWithFieldThatGetsRenamed: {"old_foo": 10}, ChildWithFieldThatGetsRenamed: {"old_foo": 3}},
            id="child_changed_field_and_parent_and_child_renamed_it_in_one_version",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                _RENAME_FOO_IN_PARENT_AND_CHILD,
                [
                    schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(name="older_foo"),
                    schema(ChildWithFieldThatGetsRenamed).field("old_foo").had(name="older_foo"),
                ],
            ],
            {ParentWithFieldThatGetsRenamed: {"older_foo": 10}, ChildWithFieldThatGetsRenamed: {"older_foo": 3}},
            id="child_changed_field_then_parent_and_child_renamed_it_twice",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                _RENAME_FOO_IN_PARENT_AND_CHILD,
                [schema(ParentWithFieldThatGetsRenamed).field("old_foo").didnt_exist],
            ],
            {ParentWithFieldThatGetsRenamed: {}, ChildWithFieldThatGetsRenamed: {"old_foo": 3}},
            id="child_changed_field_then_parent_and_child_renamed_it_then_parent_deleted_it",
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
                _RENAME_FOO_IN_PARENT_AND_CHILD,
                [schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(max_length=5)],
            ],
            {ParentWithFieldThatGetsRenamed: {"old_foo": 5}, ChildWithFieldThatGetsRenamed: {"old_foo": 5}},
            id="child_changed_field_then_parent_and_child_renamed_it_then_parent_changed_it",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                _RENAME_FOO_IN_PARENT_AND_CHILD,
                [schema(ChildWithFieldThatGetsRenamed).field("old_foo").had(max_length=5)],
            ],
            {ParentWithFieldThatGetsRenamed: {"old_foo": 10}, ChildWithFieldThatGetsRenamed: {"old_foo": 5}},
            id="child_changed_field_then_parent_and_child_renamed_it_then_child_changed_it_again",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ChildWithFieldThatGetsRenamed).field("foo").didnt_exist,
                ],
            ],
            {
                ParentWithFieldThatGetsRenamed: {"old_foo": 10},
                ChildWithFieldThatGetsRenamed: {"old_foo": 3},
                GrandchildWithFieldThatGetsRenamed: {"old_foo": 3},
            },
            id="child_changed_field_then_parent_renamed_it_and_child_deleted_it",
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


def _parent_rename_error(
    model: str, name_in_model: str, *, parent: str = "ParentWithFieldThatGetsRenamed", new_name: str = "old_foo"
) -> str:
    return (
        f'"{parent}" renamed the field "foo" to "{new_name}" in "MyVersionChange", but in version "2000-01-01" '
        f'"{model}" still has it as "{name_in_model}" next to "{new_name}", because "{model}" declares "foo" itself '
        "or changed it in a newer version. The renames of a parent don't rename the fields that its subclasses "
        f'declare. Rename it in "{model}" in the same version too: '
        f'schema({model}).field("{name_in_model}").had(name="{new_name}").'
    )


class ChildThatRedeclaresFieldThatGetsRenamed(ParentWithFieldThatGetsRenamed):
    foo: str = Field(default="", max_length=3)


class ChildWithoutInstructionsThatRedeclaresFieldThatGetsRenamed(ParentWithFieldThatGetsRenamed):
    foo: str = Field(default="", max_length=3)


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected_error"),
    [
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
            ],
            _parent_rename_error("ChildWithFieldThatGetsRenamed", "foo"),
            id="child_changed_field_then_parent_renamed_it",
        ),
        pytest.param(
            [
                [
                    schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3),
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                ]
            ],
            _parent_rename_error("ChildWithFieldThatGetsRenamed", "foo"),
            id="child_changed_field_and_parent_renamed_it_in_one_version",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(name="older_foo"),
                ],
            ],
            _parent_rename_error("ChildWithFieldThatGetsRenamed", "foo", new_name="older_foo"),
            id="child_changed_field_then_parent_renamed_it_twice_in_one_version",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ChildWithFieldThatGetsRenamed).field("foo").had(name="other_foo"),
                ],
            ],
            _parent_rename_error("ChildWithFieldThatGetsRenamed", "other_foo"),
            id="child_changed_field_then_parent_and_child_renamed_it_to_other_names",
        ),
        pytest.param(
            [
                [
                    schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3),
                    schema(ChildWithFieldThatGetsRenamed).field("foo").had(name="other_foo"),
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                ]
            ],
            _parent_rename_error("ChildWithFieldThatGetsRenamed", "other_foo"),
            id="child_changed_field_and_child_and_parent_renamed_it_to_other_names_in_one_version",
        ),
        pytest.param(
            [
                [schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3)],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
                [schema(ParentWithFieldThatGetsRenamed).field("old_foo").didnt_exist],
            ],
            _parent_rename_error("ChildWithFieldThatGetsRenamed", "foo").replace("2000-01-01", "2001-01-01"),
            id="child_changed_field_then_parent_renamed_it_then_deleted_it",
        ),
        pytest.param(
            [
                [schema(ChildThatRedeclaresFieldThatGetsRenamed).field("bar").had(description="Bar")],
                [schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")],
            ],
            _parent_rename_error("ChildThatRedeclaresFieldThatGetsRenamed", "foo"),
            id="child_with_instructions_redeclares_field_that_parent_renamed",
        ),
    ],
)
def test__schema_field_had_name__parent_renamed_field_that_subclass_has_under_another_name__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected_error: str,
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(*(version_change(*instructions) for instructions in reversed(instructions_from_newest)))

    assert str(exc_info.value) == expected_error


@pytest.mark.parametrize("parent_first", [True, False])
def test__schema_field_had_name__parent_and_redeclaring_child_renamed_field_to_other_names__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, parent_first: bool
):
    parent_rename = schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo")
    child_rename = schema(ChildThatRedeclaresFieldThatGetsRenamed).field("foo").had(name="other_foo")

    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(
            version_change(*((parent_rename, child_rename) if parent_first else (child_rename, parent_rename)))
        )

    assert str(exc_info.value) == _parent_rename_error("ChildThatRedeclaresFieldThatGetsRenamed", "other_foo")


class OtherBaseWithFloatFoo(BaseModel):
    foo: float = 0.0


class ChildOfTwoBasesThatRedeclaresFieldThatGetsRenamed(OtherBaseWithFloatFoo, ParentWithFieldThatGetsRenamed):
    foo: str = ""  # ty: ignore[invalid-attribute-override]


def test__schema_field_had_name__parent_renamed_field_that_child_of_two_bases_redeclares__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        schemas["2000-01-01"][ChildOfTwoBasesThatRedeclaresFieldThatGetsRenamed]

    assert str(exc_info.value) == _parent_rename_error("ChildOfTwoBasesThatRedeclaresFieldThatGetsRenamed", "foo")


class PlainMixinWithFoo:
    foo: str = "mixin"


class ChildOfPlainMixinWithFooAndParentThatRenamesIt(PlainMixinWithFoo, ParentWithFieldThatGetsRenamed):
    pass


class FirstBaseWithFoo(BaseModel):
    foo: str = ""


class ChildOfFirstBaseWithFooAndParentThatRenamesIt(FirstBaseWithFoo, ParentWithFieldThatGetsRenamed):
    pass


def _inherited_old_name_error(model: str, base: str, advice: str) -> str:
    return (
        f'"ParentWithFieldThatGetsRenamed" renamed the field "foo" to "old_foo" in "MyVersionChange", but in version '
        f'"2000-01-01" "{model}" still has "foo" next to "old_foo", because it inherits "foo" from "{base}". '
        f"Pydantic models can't drop the fields that they inherit. {advice}"
    )


@pytest.mark.parametrize(
    ("child", "expected_error"),
    [
        pytest.param(
            ChildOfPlainMixinWithFooAndParentThatRenamesIt,
            _inherited_old_name_error(
                "ChildOfPlainMixinWithFooAndParentThatRenamesIt",
                "PlainMixinWithFoo",
                '"PlainMixinWithFoo" is not a pydantic model, so Cadwyn can\'t rename the field in it. Declare the '
                'field in a pydantic model instead of "PlainMixinWithFoo" and rename it there.',
            ),
            id="plain_mixin",
        ),
        pytest.param(
            ChildOfFirstBaseWithFooAndParentThatRenamesIt,
            _inherited_old_name_error(
                "ChildOfFirstBaseWithFooAndParentThatRenamesIt",
                "FirstBaseWithFoo",
                'Rename it in "FirstBaseWithFoo" in the same version too.',
            ),
            id="pydantic_base",
        ),
    ],
)
@pytest.mark.filterwarnings('ignore:Field name "foo" in "ChildOfPlainMixinWithFooAndParentThatRenamesIt" shadows')
def test__schema_field_had_name__parent_renamed_field_that_other_base_of_child_overrides__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, child: type[BaseModel], expected_error: str
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        schemas["2000-01-01"][child]

    assert str(exc_info.value) == expected_error


class GenericChildThatRedeclaresInheritedField(GenericParentWithInheritedField[int]):
    kind: int


def test__schema_field_had_name__generic_parent_renamed_field_and_got_new_one_with_old_name__should_keep_both(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericParentWithInheritedField).field("kind").had(name="old_kind"),
            schema(GenericParentWithInheritedField).field("kind").existed_as(type=str, info=Field(default="")),
        )
    )

    child = schemas["2000-01-01"][GenericChildThatRedeclaresInheritedField]
    assert {name: field.annotation for name, field in child.model_fields.items()} == {
        "bar": int,
        "old_kind": int,
        "kind": int,
    }


def test__schema_field_had_name__parent_renamed_field_that_child_without_instructions_redeclares__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    # Cadwyn copies the models without instructions when something needs them, e.g. the routes when the app starts
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        schemas["2000-01-01"][ChildWithoutInstructionsThatRedeclaresFieldThatGetsRenamed]

    assert str(exc_info.value) == _parent_rename_error(
        "ChildWithoutInstructionsThatRedeclaresFieldThatGetsRenamed", "foo"
    )


_DESCRIBE_BAR_IN_REDECLARING_CHILD = schema(ChildThatRedeclaresFieldThatGetsRenamed).field("bar").had(description="Bar")


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected_fields"),
    [
        pytest.param(
            [
                [_DESCRIBE_BAR_IN_REDECLARING_CHILD],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ChildThatRedeclaresFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                ],
            ],
            {"bar": int, "old_foo": str},
            id="child_renamed_field_that_it_redeclares_too",
        ),
        pytest.param(
            [
                [_DESCRIBE_BAR_IN_REDECLARING_CHILD],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ChildThatRedeclaresFieldThatGetsRenamed).field("foo").didnt_exist,
                ],
            ],
            {"bar": int, "old_foo": str},
            id="child_deleted_field_that_it_redeclares",
        ),
        pytest.param(
            [
                [_DESCRIBE_BAR_IN_REDECLARING_CHILD],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ChildThatRedeclaresFieldThatGetsRenamed).field("foo").didnt_exist,
                ],
                [schema(ChildThatRedeclaresFieldThatGetsRenamed).field("foo").existed_as(type=int)],
            ],
            {"bar": int, "old_foo": str, "foo": int},
            id="child_got_new_field_with_old_name_in_older_version",
        ),
        pytest.param(
            [
                [_DESCRIBE_BAR_IN_REDECLARING_CHILD],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(name="foo"),
                ],
            ],
            {"foo": str, "bar": int},
            id="parent_renamed_field_back_in_same_version",
        ),
        pytest.param(
            [
                [
                    _DESCRIBE_BAR_IN_REDECLARING_CHILD,
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ParentWithFieldThatGetsRenamed).field("foo").existed_as(type=bytes, info=Field(default=b"")),
                ]
            ],
            {"bar": int, "old_foo": str, "foo": str},
            id="parent_got_new_field_with_old_name_in_same_version",
        ),
        pytest.param(
            [
                [_DESCRIBE_BAR_IN_REDECLARING_CHILD],
                [
                    schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
                    schema(ParentWithFieldThatGetsRenamed).field("old_foo").didnt_exist,
                ],
            ],
            {"bar": int, "foo": str},
            id="parent_renamed_field_and_deleted_it_in_same_version",
        ),
    ],
)
def test__schema_field_had_name__parent_renamed_field_that_child_no_longer_has_under_other_name__should_not_raise(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected_fields: dict[str, type],
):
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest))
    )

    old_child = schemas["2000-01-01"][ChildThatRedeclaresFieldThatGetsRenamed]
    assert {name: field.annotation for name, field in old_child.model_fields.items()} == expected_fields


def test__schema_field_had__parent_renamed_field_and_changed_its_type__should_change_type_of_subclass(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithFieldThatGetsRenamed).field("old_foo").had(type=bytes)),
        version_change(
            schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
            schema(ChildWithFieldThatGetsRenamed).field("foo").didnt_exist,
        ),
        version_change(
            schema(ParentWithFieldThatGetsRenamed).field("bar").had(description="Bar"),
            schema(ChildWithFieldThatGetsRenamed).field("foo").had(max_length=3),
        ),
    )

    child = schemas["2000-01-01"][ChildWithFieldThatGetsRenamed]
    assert "foo" not in child.model_fields
    assert child.model_fields["old_foo"].annotation is bytes
    assert _get_max_lengths(child)["old_foo"] == 3


def test__schema_field_had__generic_parent_renamed_field_that_subclass_changed__should_keep_its_concrete_type(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericParentWithInheritedField).field("kind").had(name="old_kind"),
            schema(GenericChildWithInheritedField).field("kind").had(name="old_kind"),
        ),
        version_change(
            schema(GenericParentWithInheritedField).field("bar").had(description="Bar"),
            schema(GenericChildWithInheritedField).field("kind").had(description="Child"),
        ),
    )

    child = schemas["2000-01-01"][GenericChildWithInheritedField]
    assert "kind" not in child.model_fields
    assert child.model_fields["old_kind"].annotation == Literal["a"]
    assert child.model_fields["old_kind"].description == "Child"


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
            schema(DiamondWithRenamedField).field("foo").didnt_exist,
        )
    )

    diamond = schemas["2000-01-01"][DiamondWithRenamedField]
    assert diamond.model_validate({"old": "abc"}).model_dump() == {"old": "abc"}


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
            schema(ChildOfMiddleWithProperty).field("foo").didnt_exist,
        )
    )

    child = schemas["2000-01-01"][ChildOfMiddleWithProperty]
    assert not child.model_fields["old"].is_required()
    assert child.model_validate({"old": "abcd"}).model_dump() == {"old": "abcd"}


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
            schema(ChildWithFieldsThatShareDefaultFactoryToRename).field("foo").didnt_exist,
            schema(ChildWithFieldsThatShareDefaultFactoryToRename).field("bar").didnt_exist,
        )
    )

    # Like the fields that the child inherits from the parent, the renamed ones share the parent's copy of the factory
    assert schemas["2000-01-01"][ChildWithFieldsThatShareDefaultFactoryToRename]().model_dump() == {
        "baz": 1,
        "old_foo": 2,
        "old_bar": 3,
    }


def _child_rename_error(model: str, inherited: str, parent: str, advice: str, version: str = "2000-01-01") -> str:
    return (
        f'You tried to rename the field "foo" of "{model}" to "old_foo" in "MyVersionChange", but in version '
        f'"{version}" "{model}" still inherits "{inherited}" from "{parent}", so it would have both fields. Pydantic '
        f"models can't drop the fields that they inherit. {advice}"
    )


def _rename_in_parent_instead(parent: str) -> str:
    return (
        f'Rename it in "{parent}" instead, which renames it in its subclasses too. Subclasses that declare the field '
        "themselves or changed it in a newer version need the same rename in the same version."
    )


class ChildOfSchemaWithOneIntField(SchemaWithOneIntField):
    pass


class MixinWithIntFoo:
    foo: int


class ParentWithStrFoo(BaseModel):
    foo: str


class ChildOfMixinAndParentWithFoo(MixinWithIntFoo, ParentWithStrFoo):
    foo: int  # ty: ignore[invalid-attribute-override]


class MixinWithFooAnnotation:
    foo: str


class ChildOfMixinWithFooAnnotation(MixinWithFooAnnotation, BaseModel):
    foo: str


class ParentWithFieldNamedLikeIntermediateRename(BaseModel):
    old_foo: int = 0


class ChildRenamedThroughNameOfParentField(ParentWithFieldNamedLikeIntermediateRename):
    foo: str


class BaseWithUnrelatedOlderFoo(BaseModel):
    older_foo: int = 0


class ChildOfBaseWithUnrelatedOlderFoo(BaseWithUnrelatedOlderFoo, InheritedFieldParent):
    pass


class AncestorWithFoo(BaseModel):
    foo: str


class ParentThatHidesFooAsClassVar(AncestorWithFoo):
    foo: ClassVar[str]  # ty: ignore[invalid-attribute-override]


class ChildThatRedeclaresFooHiddenByParent(ParentThatHidesFooAsClassVar):
    foo: str  # ty: ignore[invalid-attribute-override]


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected_error"),
    [
        pytest.param(
            [[schema(InheritedFieldChild).field("foo").had(name="old_foo")]],
            _child_rename_error(
                "InheritedFieldChild", "foo", "InheritedFieldParent", _rename_in_parent_instead("InheritedFieldParent")
            ),
            id="child_renamed_inherited_field",
        ),
        pytest.param(
            [[schema(InheritedFieldGrandchild).field("foo").had(name="old_foo")]],
            _child_rename_error(
                "InheritedFieldGrandchild",
                "foo",
                "InheritedFieldParent",
                _rename_in_parent_instead("InheritedFieldParent"),
            ),
            id="grandchild_renamed_inherited_field",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [schema(InheritedFieldGrandchild).field("foo").had(name="old_foo")],
            ],
            _child_rename_error(
                "InheritedFieldGrandchild",
                "foo",
                "InheritedFieldChild",
                _rename_in_parent_instead("InheritedFieldChild"),
            ),
            id="grandchild_renamed_field_that_child_changed",
        ),
        pytest.param(
            [
                [schema(InheritedFieldGrandchild).field("foo").had(max_length=3)],
                [schema(InheritedFieldChild).field("foo").had(name="old_foo")],
            ],
            _child_rename_error(
                "InheritedFieldChild", "foo", "InheritedFieldParent", _rename_in_parent_instead("InheritedFieldParent")
            ),
            id="grandchild_changed_field_then_child_renamed_it",
        ),
        pytest.param(
            [[schema(InheritedFieldRedeclaringChild).field("foo").had(name="old_foo")]],
            _child_rename_error(
                "InheritedFieldRedeclaringChild",
                "foo",
                "InheritedFieldParent",
                'Rename it in "InheritedFieldParent" in the same version too.',
            ),
            id="child_renamed_field_that_it_redeclares",
        ),
        pytest.param(
            [[schema(ChildOfMixinWithFooAnnotation).field("foo").had(name="old_foo")]],
            _child_rename_error(
                "ChildOfMixinWithFooAnnotation",
                "foo",
                "MixinWithFooAnnotation",
                '"MixinWithFooAnnotation" is not a pydantic model, so Cadwyn can\'t rename the field in it. Declare '
                'the field in a pydantic model instead of "MixinWithFooAnnotation" and rename it there.',
            ),
            id="child_renamed_field_that_plain_mixin_declares_too",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(name="old_foo")],
                [schema(InheritedFieldChild).field("old_foo").had(name="foo")],
            ],
            _child_rename_error(
                "InheritedFieldChild",
                "foo",
                "InheritedFieldParent",
                _rename_in_parent_instead("InheritedFieldParent"),
                version="2001-01-01",
            ),
            id="child_renamed_inherited_field_in_newer_version_than_rename_back",
        ),
        pytest.param(
            [
                [
                    schema(ChildOfSchemaWithOneIntField).field("foo").had(name="old_foo"),
                    schema(SchemaWithOneIntField).field("foo").didnt_exist,
                ],
                [schema(SchemaWithOneIntField).field("foo").existed_as(type=int)],
            ],
            _child_rename_error(
                "ChildOfSchemaWithOneIntField",
                "foo",
                "SchemaWithOneIntField",
                'Rename it in "SchemaWithOneIntField" in the same version too.',
            ),
            id="parent_deleted_field_then_got_it_back_in_older_version",
        ),
        pytest.param(
            [[schema(ChildOfMixinAndParentWithFoo).field("foo").had(name="old_foo")]],
            _child_rename_error(
                "ChildOfMixinAndParentWithFoo",
                "foo",
                "ParentWithStrFoo",
                'Rename it in "ParentWithStrFoo" in the same version too.',
            ),
            id="child_of_mixin_and_parent_renamed_field_that_both_declare",
        ),
    ],
)
def test__schema_field_had_name__child_renamed_field_that_parent_still_has__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected_error: str,
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(*(version_change(*instructions) for instructions in reversed(instructions_from_newest)))

    assert str(exc_info.value) == expected_error


@pytest.mark.parametrize(
    "instructions_from_newest",
    [
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(name="old_foo"),
                    schema(InheritedFieldParent).field("foo").didnt_exist,
                ]
            ],
            id="parent_deleted_field_in_same_version_change",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(name="old_foo"),
                    schema(InheritedFieldParent).field("foo").had(name="old_foo"),
                ]
            ],
            id="parent_renamed_field_to_same_name_in_same_version_change",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldRedeclaringChild).field("foo").had(name="old_foo"),
                    schema(InheritedFieldParent).field("foo").had(name="old_foo"),
                ]
            ],
            id="parent_renamed_field_that_child_redeclares_in_same_version_change",
        ),
        pytest.param(
            [[schema(ChildThatRedeclaresFooHiddenByParent).field("foo").had(name="old_foo")]],
            id="parent_hides_ancestor_field_with_class_var",
        ),
        pytest.param(
            [
                [
                    schema(ChildOfBaseWithUnrelatedOlderFoo).field("foo").had(name="old_foo"),
                    schema(InheritedFieldParent).field("foo").had(name="older_foo"),
                ]
            ],
            id="parent_renamed_field_to_name_that_closer_base_declares",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [
                    schema(InheritedFieldChild).field("foo").had(name="old_foo"),
                    schema(InheritedFieldParent).field("foo").had(name="old_foo"),
                ],
            ],
            id="child_changed_field_then_child_and_parent_renamed_it_in_same_version_change",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [
                    schema(InheritedFieldParent).field("foo").had(name="old_foo"),
                    schema(InheritedFieldChild).field("foo").had(name="old_foo"),
                ],
            ],
            id="child_changed_field_then_parent_and_child_renamed_it_in_same_version_change",
        ),
    ],
)
def test__schema_field_had_name__child_renamed_field_that_parent_no_longer_has__should_rename_it_in_child(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
):
    child = instructions_from_newest[-1][0].schema
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest))
    )

    assert "foo" not in schemas["2000-01-01"][child].model_fields
    assert schemas["2000-01-01"][child].model_validate({"old_foo": "v"}).old_foo == "v"


def test__schema_field_had_name__child_renamed_field_that_parent_deletes_in_another_change_of_version__should_rename():
    rename = version_change(schema(InheritedFieldChild).field("foo").had(name="old_foo"))
    delete = version_change(schema(InheritedFieldParent).field("foo").didnt_exist)
    schemas = generate_versioned_models(VersionBundle(Version("2001-01-01", rename, delete), Version("2000-01-01")))

    assert list(schemas["2000-01-01"][InheritedFieldChild].model_fields) == ["bar", "old_foo"]


@pytest.mark.parametrize(
    "version_changes",
    [
        pytest.param(
            [
                version_change(
                    schema(InheritedFieldChild).field("foo").had(name="old_foo"),
                    schema(InheritedFieldChild).field("old_foo").didnt_exist,
                )
            ],
            id="in_same_version_change",
        ),
        pytest.param(
            [
                version_change(schema(InheritedFieldChild).field("foo").had(name="old_foo")),
                version_change(schema(InheritedFieldChild).field("old_foo").didnt_exist),
            ],
            id="in_another_change_of_version",
        ),
    ],
)
def test__schema_field_had_name__child_renamed_inherited_field_and_deleted_new_name__should_keep_inherited_field(
    version_changes: list[type[VersionChange]],
):
    schemas = generate_versioned_models(VersionBundle(Version("2001-01-01", *version_changes), Version("2000-01-01")))

    child = schemas["2000-01-01"][InheritedFieldChild]
    assert list(child.model_fields) == ["foo", "bar"]
    assert child.model_validate({"foo": "v"}).foo == "v"


def test__schema_field_had_name__child_renamed_inherited_field_to_class_var__should_keep_only_inherited_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ChildOfSchemaWithOneIntField).field("foo").had(name="old_foo", type=ClassVar[int]))
    )

    child = schemas["2000-01-01"][ChildOfSchemaWithOneIntField]
    assert list(child.model_fields) == ["foo"]
    assert child.model_validate({"foo": 1}).model_dump() == {"foo": 1}


def test__schema_field_had_name__renamed_field_and_added_field_with_old_name__should_keep_both_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField).field("foo").had(name="bar"),
            schema(SchemaWithOneStrField).field("foo").existed_as(type=int),
        )
    )

    model = schemas["2000-01-01"][SchemaWithOneStrField]
    assert model.model_validate({"foo": 1, "bar": "v"}).model_dump() == {"bar": "v", "foo": 1}


def test__schema_field_had_name__child_renamed_field_through_name_of_parent_field__should_keep_both_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildRenamedThroughNameOfParentField).field("foo").had(name="old_foo"),
            schema(ChildRenamedThroughNameOfParentField).field("old_foo").had(name="older_foo"),
        )
    )

    child = schemas["2000-01-01"][ChildRenamedThroughNameOfParentField]
    assert child.model_validate({"old_foo": 1, "older_foo": "v"}).model_dump() == {"old_foo": 1, "older_foo": "v"}
    assert "foo" not in child.model_fields


class ParentOfChildThatRetypesFoo(BaseModel):
    foo: str = Field(description="Parent", max_length=5)


class ChildThatRetypesFoo(ParentOfChildThatRetypesFoo):
    foo: Annotated[int, Field(gt=0)] = Field(description="Child")  # ty: ignore[invalid-attribute-override]


def test__schema_field_existed_as__changelog_recipe_for_child_rename__should_keep_parent_field_and_add_child_field(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    # The CHANGELOG of 9.0 gives this recipe to keep the older versions of 8.x, in which
    # schema(ChildThatRetypesFoo).field("foo").had(name="old_foo") inherited the parent's foo next to old_foo
    schemas = create_runtime_schemas(
        version_change(
            schema(ChildThatRetypesFoo)
            .field("old_foo")
            .existed_as(type=Annotated[int, Field(gt=0)], info=Field(description="Child")),
            schema(ChildThatRetypesFoo).field("foo").had(type=str, description="Parent", max_length=5),
            schema(ChildThatRetypesFoo).field("foo").didnt_have("gt"),
        )
    )

    child = schemas["2000-01-01"][ChildThatRetypesFoo]
    assert child.model_validate({"foo": "text", "old_foo": 1}).model_dump() == {"foo": "text", "old_foo": 1}
    assert {error["loc"] for error in _get_validation_errors(child, {"foo": "toolong", "old_foo": 0})} == {
        ("foo",),
        ("old_foo",),
    }


def _get_validation_errors(model: type[BaseModel], data: dict[str, Any]) -> list[Any]:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(data)
    return exc_info.value.errors()


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


class TagList(list[Any]):
    pass


class TagTuple(tuple[Any, ...]):  # noqa: SLOT001 # It needs instance attributes
    pass


class Options(dict[str, Any]):
    pass


class Point(NamedTuple):
    x: Any
    y: Any


def _with_source(container: _T) -> _T:
    container.source = "config"  # ty: ignore[unresolved-attribute]
    return container


def _get_default(schemas: dict[str, SchemaGenerator], version: str, model: type[BaseModel]) -> Any:
    return schemas[version][model].model_fields["foo"].default


@pytest.mark.parametrize(
    "default",
    [
        pytest.param(_with_source(TagList(["a"])), id="list_subclass"),
        pytest.param(_with_source(TagTuple(("a",))), id="tuple_subclass"),
        pytest.param(_with_source(Options(a=[1])), id="dict_subclass"),
        pytest.param(collections.defaultdict(list, a=[1]), id="defaultdict"),
        pytest.param(_with_source(collections.OrderedDict(a=[1])), id="ordered_dict"),
        pytest.param(collections.Counter(a=1), id="counter"),  # deepcopy drops the attributes of Counter
        pytest.param(Point(1, [2]), id="named_tuple"),
    ],
)
def test__schema_field__default_that_subclasses_list_tuple_or_dict__should_get_a_deep_copy_in_every_version(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
):
    model = create_model("ModelWithContainerSubclassDefault", foo=(Any, default), bar=(int, 0))
    schemas = create_runtime_schemas(version_change(schema(model).field("bar").had(description="Bar")))

    defaults = [_get_default(schemas, version, model) for version in ("2000-01-01", "2001-01-01")]
    for value in defaults:
        assert type(value) is type(default)
        assert value == default
        assert getattr(value, "__dict__", None) == getattr(default, "__dict__", None)
        assert getattr(value, "default_factory", None) is getattr(default, "default_factory", None)
        assert value is not default
    assert defaults[0] is not defaults[1]


class CopyMarkingList(list[Any]):
    def __deepcopy__(self, memo: dict[int, Any]) -> "CopyMarkingList":
        result = CopyMarkingList(copy.deepcopy(list(self), memo))
        result.copied_by_its_own_hook = True  # ty: ignore[unresolved-attribute]
        return result


def test__schema_field__default_that_subclasses_list_with_own_deepcopy__should_be_copied_with_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    model = create_model("ModelWithOwnDeepcopyDefault", foo=(Any, CopyMarkingList([1])))
    schemas = create_runtime_schemas(version_change())

    value = _get_default(schemas, "2000-01-01", model)
    assert value == [1]
    assert value.copied_by_its_own_hook


def _make_self_containing_list() -> list[Any]:
    default: list[Any] = [1, list[int]]
    default.append(default)
    return default


def _make_self_containing_dict(factory: Callable[[], dict[Any, Any]]) -> dict[Any, Any]:
    default = factory()
    default["self"] = default
    return default


def _make_tuple_that_contains_itself() -> tuple[Any, ...]:
    child: list[Any] = []
    default = (1, child)
    child.append(default)
    return default


def _make_self_containing_list_subclass() -> TagList:
    default = _with_source(TagList([1]))
    default.append(default)
    return default


@pytest.mark.parametrize(
    ("default", "get_itself"),
    [
        pytest.param(_make_self_containing_list(), lambda value: value[2], id="list"),
        pytest.param(_make_self_containing_dict(dict), lambda value: value["self"], id="dict"),
        pytest.param(_make_tuple_that_contains_itself(), lambda value: value[1][0], id="tuple_through_list"),
        pytest.param(_make_self_containing_list_subclass(), lambda value: value[1], id="list_subclass"),
        pytest.param(
            _make_self_containing_dict(collections.OrderedDict), lambda value: value["self"], id="ordered_dict"
        ),
        pytest.param(
            _make_self_containing_dict(lambda: collections.defaultdict(list)),
            lambda value: value["self"],
            id="defaultdict",
        ),
    ],
)
def test__schema_field__default_that_contains_itself__should_keep_containing_itself_in_its_own_copy(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
    get_itself: Callable[[Any], Any],
):
    model = create_model("ModelWithSelfContainingDefault", foo=(Any, default))
    schemas = create_runtime_schemas(version_change())

    defaults = [_get_default(schemas, version, model) for version in ("2000-01-01", "2001-01-01")]
    for value in defaults:
        assert type(value) is type(default)
        assert get_itself(value) is value
        assert value is not default
    assert defaults[0] is not defaults[1]


Ints = TypeAliasType("Ints", list[int])


@pytest.mark.parametrize(
    "default",
    [
        pytest.param(TagList([list[int]]), id="builtin_generic_alias"),
        pytest.param(TagList([typing.List[int]]), id="typing_generic_alias"),  # noqa: UP006
        pytest.param(Options(value=Annotated[int, []]), id="annotated_with_unhashable_metadata"),
        pytest.param(TagList([Ints]), id="type_alias"),
        pytest.param(TagList([int | str]), id="union_type"),
        pytest.param(Point(1, Union[int, str]), id="typing_union"),
        pytest.param(TagList([MyEnum]), id="enum_without_instructions"),
    ],
)
def test__schema_field__container_subclass_default_with_nothing_to_version__should_stay_the_same(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
):
    model = create_model("ModelWithTypingObjectsInContainerSubclassDefault", foo=(Any, default))
    schemas = create_runtime_schemas(version_change())

    value = _get_default(schemas, "2000-01-01", model)
    assert type(value) is type(default)
    assert value == default


@pytest.mark.parametrize(
    "default",
    [
        pytest.param([1], id="list"),
        pytest.param({"a": [1]}, id="nested_in_dict"),
        pytest.param((1, [1]), id="nested_in_tuple"),
        pytest.param(_with_source(TagList([1])), id="list_subclass"),
        pytest.param(Options(a=[1]), id="nested_in_dict_subclass"),
        pytest.param(Point(1, [1]), id="nested_in_named_tuple"),
    ],
)
def test__schema_field__mutating_default_of_one_version__should_not_change_other_versions_or_head(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
):
    model = create_model("ModelWithMutableContainerDefault", foo=(Any, copy.deepcopy(default)))
    schemas = create_runtime_schemas(version_change())

    defaults = [_get_default(schemas, version, model) for version in ("2000-01-01", "2001-01-01")]
    _get_innermost_list(defaults[0]).append(2)

    assert defaults[1] == default
    assert model.model_fields["foo"].default == default


def _get_innermost_list(value: Any) -> list[Any]:
    while not isinstance(value, list):
        value = list(value.values())[-1] if isinstance(value, dict) else value[-1]
    return value


class Sentinel:
    pass


SENTINEL = Sentinel()


@pytest.mark.parametrize(
    "default",
    [
        pytest.param([SENTINEL], id="list"),
        pytest.param((SENTINEL,), id="tuple"),
        pytest.param({"a": SENTINEL}, id="dict"),
    ],
)
def test__schema_field__plain_container_default__should_share_the_objects_within_it_with_head(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
):
    model = create_model(
        "ModelWithSentinelInDefault", __config__=ConfigDict(arbitrary_types_allowed=True), foo=(Any, default)
    )
    schemas = create_runtime_schemas(version_change())

    value = _get_default(schemas, "2000-01-01", model)
    assert type(value) is type(default)
    assert next(iter(value.values() if isinstance(value, dict) else value)) is SENTINEL


@pytest.mark.parametrize(
    ("make_default", "get_class"),
    [
        pytest.param(lambda cls: [cls], lambda default: default[0], id="list"),
        pytest.param(lambda cls: (1, cls), lambda default: default[1], id="tuple"),
        pytest.param(lambda cls: {"model": cls}, lambda default: default["model"], id="dict"),
    ],
)
def test__schema_field__plain_container_default_with_versioned_model_class__should_use_the_class_of_each_version(
    create_runtime_schemas: CreateRuntimeSchemas,
    make_default: Callable[[type[BaseModel]], Any],
    get_class: Callable[[Any], Any],
):
    model = create_model("ModelWithModelClassInDefault", foo=(Any, make_default(SchemaWithOneStrField)))
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").had(description="Old")))

    descriptions = {}
    for version in ("2000-01-01", "2001-01-01"):
        model_class = get_class(schemas[version][model]().foo)  # ty: ignore[unresolved-attribute]
        assert model_class is schemas[version][SchemaWithOneStrField]
        descriptions[version] = model_class.model_fields["foo"].description
    assert descriptions == {"2000-01-01": "Old", "2001-01-01": None}


def _function_in_default(value: int) -> int:  # pragma: no cover # Only its signature is versioned
    return value


@pytest.mark.parametrize(
    "default",
    [
        pytest.param(TagList([SchemaWithOneStrField]), id="list_subclass"),
        pytest.param(Options(model=SchemaWithOneStrField), id="dict_subclass"),
        pytest.param(Point(1, SchemaWithOneStrField), id="named_tuple"),
        pytest.param(collections.OrderedDict(model=SchemaWithOneStrField), id="ordered_dict"),
        pytest.param(collections.Counter({SchemaWithOneStrField: 1}), id="counter"),
        pytest.param(collections.defaultdict[str, Any](list, model=SchemaWithOneStrField), id="defaultdict"),
        pytest.param(TagList([list[SchemaWithOneStrField]]), id="model_in_generic_alias"),
        pytest.param(TagList([Annotated[int, SchemaWithOneStrField]]), id="model_in_annotated_metadata"),
        pytest.param(TagList([MyEnum]), id="enum_with_instructions"),
        pytest.param(TagList([_function_in_default]), id="function"),
    ],
)
def test__schema_field__container_subclass_default_with_values_to_version__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
):
    model = create_model("ModelWithModelClassInContainerSubclassDefault", foo=(Any, default))
    schemas = create_runtime_schemas(
        version_change(
            schema(SchemaWithOneStrField).field("foo").had(description="Old"),
            enum(MyEnum).didnt_have("baz"),
        )
    )

    with pytest.raises(
        SchemaGenerationError,
        match=re.escape(
            "in the field 'foo' of ModelWithModelClassInContainerSubclassDefault because it is a "
            f"{type(default).__qualname__}, not a plain list, tuple, or dict"
        ),
    ):
        schemas["2000-01-01"][model]


def test__schema_field__self_containing_default_with_versioned_model_class__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    default: list[Any] = [SchemaWithOneStrField]
    default.append(default)
    model = create_model("ModelWithModelClassInSelfContainingDefault", foo=(Any, default))
    schemas = create_runtime_schemas(version_change(schema(SchemaWithOneStrField).field("foo").had(description="Old")))

    with pytest.raises(
        SchemaGenerationError,
        match=re.escape("in the field 'foo' of ModelWithModelClassInSelfContainingDefault because it contains itself"),
    ):
        schemas["2000-01-01"][model]


def _make_list_subclass_with_lock() -> TagList:
    default = TagList([1])
    default.lock = threading.Lock()  # ty: ignore[unresolved-attribute]
    return default


@pytest.mark.parametrize(
    "default",
    [
        pytest.param(_make_list_subclass_with_lock(), id="list_subclass_with_lock"),
        pytest.param(_make_self_containing_dict(collections.Counter), id="self_containing_counter"),
    ],
)
def test__schema_field__container_subclass_default_that_cannot_be_deep_copied__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    default: Any,
):
    model = create_model(
        "ModelWithDefaultThatCannotBeDeepCopied",
        __config__=ConfigDict(arbitrary_types_allowed=True),
        foo=(Any, default),
    )
    schemas = create_runtime_schemas(version_change())

    with pytest.raises(
        SchemaGenerationError,
        match=re.escape("in the field 'foo' of ModelWithDefaultThatCannotBeDeepCopied because it cannot deep copy it"),
    ):
        schemas["2000-01-01"][model]


def test__schema_field__default_of_changed_model_that_cannot_be_deep_copied__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    model = create_model(
        "ModelWithInstructionsAndDefaultThatCannotBeDeepCopied",
        __config__=ConfigDict(arbitrary_types_allowed=True),
        foo=(Any, _make_list_subclass_with_lock()),
    )

    with pytest.raises(
        SchemaGenerationError,
        match=re.escape(
            "in the field 'foo' of ModelWithInstructionsAndDefaultThatCannotBeDeepCopied because it cannot deep copy it"
        ),
    ):
        create_runtime_schemas(version_change(schema(model).field("foo").had(description="Old")))


class ShallowCopyList(list[Any]):
    def __deepcopy__(self, memo: dict[int, Any]) -> "ShallowCopyList":
        return ShallowCopyList(self)


def test__schema_field__container_subclass_default_whose_deepcopy_shares_what_cannot_be_copied__should_use_its_copy(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    default = ShallowCopyList([_make_list_subclass_with_lock()])
    model = create_model(
        "ModelWithShallowCopyDefault", __config__=ConfigDict(arbitrary_types_allowed=True), foo=(Any, default)
    )
    schemas = create_runtime_schemas(version_change())

    for version in ("2000-01-01", "2001-01-01"):
        value = _get_default(schemas, version, model)
        assert type(value) is ShallowCopyList
        assert value is not default
        assert value[0] is default[0]


def make_tags_from_registry(registry: Any = None) -> list[str]:  # pragma: no cover # Generation fails first
    return []


TAG_FACTORIES = {"tags": make_tags_from_registry}
make_tags_from_registry.__defaults__ = (TAG_FACTORIES,)


def test__schema_field__default_factory_whose_default_contains_the_factory__should_raise_schema_generation_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class ModelWithFactoryFromRegistry(BaseModel):
        tags: list[str] = Field(default_factory=make_tags_from_registry)

    schemas = create_runtime_schemas(version_change())

    with pytest.raises(
        SchemaGenerationError,
        match=re.escape("in the parameters of make_tags_from_registry because it contains itself"),
    ):
        schemas["2000-01-01"][ModelWithFactoryFromRegistry]


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


def test__schema_field_had__annotated_default_factory_with_assigned_default__should_raise_pydantic_error():
    # Cadwyn relies on pydantic rejecting such declarations, so only instructions can give a field both
    with pytest.raises(TypeError, match="cannot specify both default and default_factory"):

        class ModelWithAnnotatedFactoryAndAssignedDefault(BaseModel):
            foo: Annotated[int, Field(default_factory=int)] = 5
            bar: int = 0


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
            "minLength": 1,
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
# Since Cadwyn 9.0, didnt_have("alias") removes the alias and the keys that pydantic copied from it, as for a field
# whose Field() isn't in the annotation (#465)
ATTRIBUTES_THAT_DIDNT_HAVE_OVERRIDES_IN_FIELD_IN_ANNOTATED = {"alias"}


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
def test__schema_field_didnt_have__attribute_of_field_in_annotated__should_only_remove_documentation_and_alias(
    create_runtime_schemas: CreateRuntimeSchemas,
    attr_name: Any,
):
    field_type, field_kwargs = ANNOTATED_FIELDS_WITH_EVERY_ATTRIBUTE[attr_name]

    class SchemaWithAttributeInAnnotated(BaseModel):
        foo: Annotated[field_type, Field(**field_kwargs)]  # ty: ignore[invalid-type-form]

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithAttributeInAnnotated).field("foo").didnt_have(attr_name)),
    )
    head_field = SchemaWithAttributeInAnnotated.model_fields["foo"]
    old_field = schemas["2000-01-01"][SchemaWithAttributeInAnnotated].model_fields["foo"]

    head_value = _get_field_attribute(head_field, attr_name)
    assert head_value not in (None, PydanticUndefined)
    if attr_name in ATTRIBUTES_THAT_DIDNT_HAVE_REMOVES_FROM_FIELD_IN_ANNOTATED | (
        ATTRIBUTES_THAT_DIDNT_HAVE_OVERRIDES_IN_FIELD_IN_ANNOTATED
    ):
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


def _delete_inherited_field_error(model: str, parent: str, advice: str, version: str = "2000-01-01") -> str:
    return (
        f'You tried to delete the field "foo" from "{model}" in "MyVersionChange", but in version "{version}" '
        f'"{model}" still inherits "foo" from "{parent}", so it would still have the field. Pydantic models can\'t '
        f"drop the fields that they inherit. {advice}"
    )


def _delete_in_parent_too(parent: str) -> str:
    return f'Delete it from "{parent}" in the same version too, which deletes it from its subclasses too.'


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected_error"),
    [
        pytest.param(
            [[schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist]],
            _delete_inherited_field_error(
                "InheritedFieldRedeclaringChild",
                "InheritedFieldParent",
                _delete_in_parent_too("InheritedFieldParent"),
            ),
            id="child_redeclares_field",
        ),
        pytest.param(
            [
                [schema(InheritedFieldChild).field("foo").had(description="Child")],
                [schema(InheritedFieldChild).field("foo").didnt_exist],
            ],
            _delete_inherited_field_error(
                "InheritedFieldChild",
                "InheritedFieldParent",
                _delete_in_parent_too("InheritedFieldParent"),
            ),
            id="child_changed_field_in_newer_version",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldChild).field("foo").had(description="Child"),
                    schema(InheritedFieldChild).field("foo").didnt_exist,
                ]
            ],
            _delete_inherited_field_error(
                "InheritedFieldChild",
                "InheritedFieldParent",
                _delete_in_parent_too("InheritedFieldParent"),
            ),
            id="child_changed_field_in_same_version_change",
        ),
        pytest.param(
            [
                [schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist],
                [schema(InheritedFieldParent).field("foo").didnt_exist],
            ],
            _delete_inherited_field_error(
                "InheritedFieldRedeclaringChild",
                "InheritedFieldParent",
                _delete_in_parent_too("InheritedFieldParent"),
                version="2001-01-01",
            ),
            id="parent_deleted_field_in_older_version",
        ),
        pytest.param(
            [[schema(ChildOfMixinWithFooAnnotation).field("foo").didnt_exist]],
            _delete_inherited_field_error(
                "ChildOfMixinWithFooAnnotation",
                "MixinWithFooAnnotation",
                '"MixinWithFooAnnotation" is not a pydantic model, so Cadwyn can\'t delete the field from it. Declare '
                'the field in a pydantic model instead of "MixinWithFooAnnotation" and delete it there.',
            ),
            id="plain_mixin_declares_field",
        ),
    ],
)
def test__schema_field_didnt_exist__base_still_has_field__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected_error: str,
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(*(version_change(*instructions) for instructions in reversed(instructions_from_newest)))

    assert str(exc_info.value) == expected_error


@pytest.mark.parametrize(
    ("instructions_from_newest", "expected_fields"),
    [
        pytest.param(
            [
                [
                    schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist,
                    schema(InheritedFieldParent).field("foo").didnt_exist,
                ]
            ],
            {"bar": int},
            id="parent_deleted_field_in_same_version_change",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldParent).field("foo").didnt_exist,
                    schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist,
                ]
            ],
            {"bar": int},
            id="parent_deleted_field_earlier_in_same_version_change",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist,
                    schema(InheritedFieldParent).field("foo").had(name="old_foo"),
                ]
            ],
            {"bar": int, "old_foo": str},
            id="parent_renamed_field_in_same_version_change",
        ),
        pytest.param(
            [
                [
                    schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist,
                    schema(InheritedFieldParent).field("foo").didnt_exist,
                ],
                [schema(InheritedFieldParent).field("foo").existed_as(type=int, info=Field(default=0))],
            ],
            {"bar": int, "foo": int},
            id="parent_got_field_back_in_older_version",
        ),
    ],
)
def test__schema_field_didnt_exist__child_redeclares_field_that_parent_no_longer_has__should_delete_it(
    create_runtime_schemas: CreateRuntimeSchemas,
    instructions_from_newest: list[list[Any]],
    expected_fields: dict[str, type],
):
    schemas = create_runtime_schemas(
        *(version_change(*instructions) for instructions in reversed(instructions_from_newest))
    )

    old_child = schemas["2000-01-01"][InheritedFieldRedeclaringChild]
    assert {name: field.annotation for name, field in old_child.model_fields.items()} == expected_fields


def test__schema_field_didnt_exist__parent_deletes_field_in_another_change_of_version__should_delete_it():
    delete_in_child = version_change(schema(InheritedFieldRedeclaringChild).field("foo").didnt_exist)
    delete_in_parent = version_change(schema(InheritedFieldParent).field("foo").didnt_exist)
    schemas = generate_versioned_models(
        VersionBundle(Version("2001-01-01", delete_in_child, delete_in_parent), Version("2000-01-01"))
    )

    assert list(schemas["2000-01-01"][InheritedFieldRedeclaringChild].model_fields) == ["bar"]


class ParentWithIntFooThatGetsRenamed(BaseModel):
    foo: int


class ChildThatRedeclaresIntFooThatGetsRenamed(ParentWithIntFooThatGetsRenamed):
    foo: int


@pytest.mark.parametrize("child_has_instructions", [True, False])
def test__schema_field_had_name__parent_renamed_field_to_class_var_and_back_to_field__should_raise_error(
    child_has_instructions: bool,
):
    renamed_to_class_var = [
        schema(ParentWithIntFooThatGetsRenamed).field("foo").had(name="old_foo", type=ClassVar[int])
    ]
    if child_has_instructions:
        renamed_to_class_var.append(schema(ChildThatRedeclaresIntFooThatGetsRenamed).had(description="Old child"))
    versions = VersionBundle(
        Version("2002-01-01", version_change(*renamed_to_class_var)),
        Version("2001-01-01", version_change(schema(ParentWithIntFooThatGetsRenamed).field("old_foo").had(type=int))),
        Version("2000-01-01"),
    )

    # The rename only gives the parent a field with the new name in the oldest version
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        generate_versioned_models(versions)["2000-01-01"][ChildThatRedeclaresIntFooThatGetsRenamed]

    assert str(exc_info.value) == (
        '"ParentWithIntFooThatGetsRenamed" renamed the field "foo" to "old_foo" in "MyVersionChange", but in version '
        '"2000-01-01" "ChildThatRedeclaresIntFooThatGetsRenamed" still has it as "foo" next to "old_foo", because '
        '"ChildThatRedeclaresIntFooThatGetsRenamed" declares "foo" itself or changed it in a newer version. The '
        "renames of a parent don't rename the fields that its subclasses declare. Rename it in "
        '"ChildThatRedeclaresIntFooThatGetsRenamed" in the same version too: '
        'schema(ChildThatRedeclaresIntFooThatGetsRenamed).field("foo").had(name="old_foo").'
    )


class RootOfDiamondWithConstrainedFoo(BaseModel):
    foo: int = Field(default=0, description="Root", ge=0)


class LeftOfDiamondWithConstrainedFoo(RootOfDiamondWithConstrainedFoo):
    pass


class RightOfDiamondWithConstrainedFoo(RootOfDiamondWithConstrainedFoo):
    foo: int = Field(default=0, description="Right", ge=10)


class DiamondWithConstrainedFoo(LeftOfDiamondWithConstrainedFoo, RightOfDiamondWithConstrainedFoo):
    foo: int = Field(default=0, description="Diamond", ge=20)


class RootOfDiamondWithPlainFoo(BaseModel):
    foo: int


class LeftOfDiamondWithPlainFoo(RootOfDiamondWithPlainFoo):
    pass


class RightOfDiamondWithPlainFoo(RootOfDiamondWithPlainFoo):
    foo: int


class DiamondWithPlainFoo(LeftOfDiamondWithPlainFoo, RightOfDiamondWithPlainFoo):
    foo: int


@pytest.mark.parametrize(
    ("diamond", "bases"),
    [
        pytest.param(DiamondWithConstrainedFoo, '"RootOfDiamondWithConstrainedFoo"', id="kept_base_is_known"),
        # Both declare `foo: int`, so we can't tell which field pydantic kept
        pytest.param(
            DiamondWithPlainFoo, 'one of "RightOfDiamondWithPlainFoo", "RootOfDiamondWithPlainFoo"', id="ambiguous"
        ),
    ],
)
def test__schema_field_didnt_exist__diamond_redeclares_field__should_name_bases_that_give_it(
    create_runtime_schemas: CreateRuntimeSchemas, diamond: type[BaseModel], bases: str
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(version_change(schema(diamond).field("foo").didnt_exist))

    assert f'still inherits "foo" from {bases}, so it would still have the field.' in str(exc_info.value)


def test__schema_field_had_name__diamond_renamed_field_that_it_redeclares__should_name_base_that_gives_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(version_change(schema(DiamondWithConstrainedFoo).field("foo").had(name="old_foo")))

    assert str(exc_info.value) == _child_rename_error(
        "DiamondWithConstrainedFoo",
        "foo",
        "RootOfDiamondWithConstrainedFoo",
        'Rename it in "RootOfDiamondWithConstrainedFoo" in the same version too.',
    )


class OtherParentWithFooThatGetsRenamed(BaseModel):
    foo: int


class DiamondWithConstrainedFooAndOtherParent(
    LeftOfDiamondWithConstrainedFoo, RightOfDiamondWithConstrainedFoo, OtherParentWithFooThatGetsRenamed
):
    pass


def test__schema_field_had_name__parent_renamed_field_that_diamond_gets_from_other_bases__should_name_kept_base(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(OtherParentWithFooThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        schemas["2000-01-01"][DiamondWithConstrainedFooAndOtherParent]

    assert 'because it inherits "foo" from "RootOfDiamondWithConstrainedFoo".' in str(exc_info.value)


def test__schema_field_had_name__diamond_renamed_field_that_bases_declare_alike__should_name_all_of_them(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(version_change(schema(DiamondWithPlainFoo).field("foo").had(name="old_foo")))

    assert str(exc_info.value) == (
        'You tried to rename the field "foo" of "DiamondWithPlainFoo" to "old_foo" in "MyVersionChange", but in '
        'version "2000-01-01" "DiamondWithPlainFoo" still inherits "foo" from one of "RightOfDiamondWithPlainFoo", '
        '"RootOfDiamondWithPlainFoo", so it would have both fields. Pydantic models can\'t drop the fields that they '
        "inherit. Cadwyn can't tell which of them pydantic took the field from, so rename it in all of them in the "
        "same version too."
    )


class DiamondWithPlainFooAndOtherParent(
    LeftOfDiamondWithPlainFoo, RightOfDiamondWithPlainFoo, OtherParentWithFooThatGetsRenamed
):
    pass


def test__schema_field_had_name__parent_renamed_field_that_diamond_gets_from_bases_alike__should_name_all_of_them(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(OtherParentWithFooThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        schemas["2000-01-01"][DiamondWithPlainFooAndOtherParent]

    assert str(exc_info.value).endswith(
        'because it inherits "foo" from one of "RightOfDiamondWithPlainFoo", "RootOfDiamondWithPlainFoo". Pydantic '
        "models can't drop the fields that they inherit. Cadwyn can't tell which of them pydantic took the field "
        "from, so rename it in all of them in the same version too."
    )


class FirstBaseWithInstructionsAndFoo(BaseModel):
    foo: str = ""
    bar: int = 0


class FirstBaseWithInstructionsWithoutFoo(BaseModel):
    bar: int = 0


class ChildOfFirstBaseWithInstructionsAndFoo(FirstBaseWithInstructionsAndFoo, ParentWithFieldThatGetsRenamed):
    pass


class ChildOfFirstBaseWithInstructionsWithoutFoo(FirstBaseWithInstructionsWithoutFoo, ParentWithFieldThatGetsRenamed):
    pass


def test__schema_field_had_name__parent_renamed_field_that_other_base_with_instructions_has__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(FirstBaseWithInstructionsAndFoo).field("bar").had(description="Bar"),
            schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
        )
    )

    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        schemas["2000-01-01"][ChildOfFirstBaseWithInstructionsAndFoo]

    assert str(exc_info.value).endswith(
        'because it inherits "foo" from "FirstBaseWithInstructionsAndFoo". Pydantic models can\'t drop the fields '
        'that they inherit. Rename it in "FirstBaseWithInstructionsAndFoo" in the same version too.'
    )


def test__schema_field_had_name__parent_renamed_field_and_other_base_got_new_one_with_old_name__should_keep_both(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ParentWithFieldThatGetsRenamed).field("foo").had(name="old_foo"),
            schema(FirstBaseWithInstructionsWithoutFoo).field("foo").existed_as(type=bytes, info=Field(default=b"")),
        )
    )

    child = schemas["2000-01-01"][ChildOfFirstBaseWithInstructionsWithoutFoo]
    assert {name: field.annotation for name, field in child.model_fields.items()} == {
        "bar": int,
        "old_foo": str,
        "foo": bytes,
    }


class ChildThatRedeclaresIntFooAndHasOldFoo(ParentWithIntFooThatGetsRenamed):
    foo: int
    old_foo: str


def test__schema_field_had_name__parent_renamed_field_to_name_that_child_declares__should_keep_child_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithIntFooThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    # The child's own `old_foo` hides the parent's renamed field, so the child has it only once, as `foo`
    child = schemas["2000-01-01"][ChildThatRedeclaresIntFooAndHasOldFoo]
    assert child.model_validate({"foo": 1, "old_foo": "own"}).model_dump() == {"old_foo": "own", "foo": 1}


class AliasOfParentWithIntFooThatGetsRenamed:
    def __mro_entries__(self, bases: tuple[Any, ...]) -> tuple[type, ...]:
        return (ParentWithIntFooThatGetsRenamed,)


class ChildOfAliasOfParentWithIntFooThatGetsRenamed(AliasOfParentWithIntFooThatGetsRenamed()):
    pass


class RedeclaringChildOfAliasOfParentWithIntFooThatGetsRenamed(AliasOfParentWithIntFooThatGetsRenamed()):
    foo: int


def test__schema_field_had_name__parent_renamed_field_that_child_of_mro_entries_base_changed__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError) as exc_info:
        create_runtime_schemas(
            version_change(
                schema(ChildOfAliasOfParentWithIntFooThatGetsRenamed).field("foo").had(description="Child"),
                schema(ParentWithIntFooThatGetsRenamed).field("foo").had(name="old_foo"),
            )
        )

    assert '"ChildOfAliasOfParentWithIntFooThatGetsRenamed" still has it as "foo" next to "old_foo"' in str(
        exc_info.value
    )


def test__schema_field_had_name__parent_renamed_field_that_child_of_mro_entries_base_redeclares__should_keep_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    # A known limitation: the copies of the subclasses of an object with `__mro_entries__` inherit from the parent
    # itself instead of its copy, so the parent's instructions, including its renames, don't reach them
    schemas = create_runtime_schemas(
        version_change(schema(ParentWithIntFooThatGetsRenamed).field("foo").had(name="old_foo"))
    )

    child = schemas["2000-01-01"][RedeclaringChildOfAliasOfParentWithIntFooThatGetsRenamed]
    assert ParentWithIntFooThatGetsRenamed in child.__mro__
    assert list(child.model_fields) == ["foo"]


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
            'You tried to change the attribute "default" of field "foo" of "SchemaWithOneStrField" to \'wow\' '
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
            'You tried to change the attribute "gt" of field "foo" of "EmptySchema" to 8 '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_runtime_schemas(
            version_change(schema(EmptySchema).field("foo").had(gt=8)),
            version_change(
                schema(EmptySchema).field("foo").existed_as(type=int, info=Field(gt=8, lt=12)),
            ),
        )


class AlwaysEqual:
    def __init__(self, name: str) -> None:
        self.name = name

    @override
    def __eq__(self, other: object) -> bool:
        return True  # pragma: no cover

    @override
    def __repr__(self) -> str:
        return f"AlwaysEqual({self.name!r})"

    __hash__ = object.__hash__


nan = float("nan")


@pytest.mark.parametrize(
    ("old_value", "new_value"),
    [
        pytest.param(1, True, id="int_to_bool"),
        pytest.param(1, 1.0, id="int_to_float"),
        pytest.param(0.0, -0.0, id="zero_to_negative_zero"),
        pytest.param(float("nan"), float("nan"), id="nan_to_another_nan"),
        pytest.param(complex(1, 0.0), complex(1, -0.0), id="complex_with_zero_to_negative_zero"),
        pytest.param(Decimal(1), Decimal("1.0"), id="decimal_to_decimal_with_another_exponent"),
        pytest.param(Decimal("NaN"), Decimal("NaN"), id="decimal_nan_to_another_decimal_nan"),
        pytest.param((1, 2), (1, True), id="tuple_with_int_to_tuple_with_bool"),
        pytest.param([1], [1, 2], id="list_to_longer_list"),
        pytest.param({"a": 1}, {"a": True}, id="dict_with_int_value_to_dict_with_bool_value"),
        pytest.param({1: "a"}, {True: "a"}, id="dict_with_int_key_to_dict_with_bool_key"),
        pytest.param({"a": 1, "b": 2}, {"b": 2, "a": 1}, id="dict_to_dict_in_another_order"),
        pytest.param(frozenset({1}), frozenset({True}), id="frozenset_with_int_to_frozenset_with_bool"),
        pytest.param({(1, frozenset({2}))}, {(1, frozenset({True}))}, id="set_with_nested_int_to_set_with_nested_bool"),
        pytest.param(AlwaysEqual("old"), AlwaysEqual("new"), id="object_with_custom_eq_to_another_object"),
    ],
)
def test__schema_field_had__default_that_equals_the_current_one_but_differs__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas, old_value: Any, new_value: Any
):
    class SchemaWithDefault(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        foo: Any = old_value

    schemas = create_runtime_schemas(version_change(schema(SchemaWithDefault).field("foo").had(default=new_value)))

    def describe(value: Any) -> tuple[type, str]:
        return type(value), repr(value)

    assert describe(schemas["2000-01-01"][SchemaWithDefault].model_fields["foo"].default) == describe(new_value)
    assert describe(schemas["2001-01-01"][SchemaWithDefault].model_fields["foo"].default) == describe(old_value)


def test__schema_field_had__default_that_equals_the_current_one_but_differs__should_change_it_in_openapi(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithIntDefault(BaseModel):
        foo: Union[int, bool] = 1

    schemas = create_runtime_schemas(version_change(schema(SchemaWithIntDefault).field("foo").had(default=True)))

    assert schemas["2000-01-01"][SchemaWithIntDefault].model_json_schema()["properties"]["foo"]["default"] is True
    assert schemas["2001-01-01"][SchemaWithIntDefault].model_json_schema()["properties"]["foo"]["default"] == 1


def test__schema_field_had__constraint_that_equals_the_current_one_but_differs__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithIntConstraint(BaseModel):
        foo: float = Field(gt=1)

    schemas = create_runtime_schemas(version_change(schema(SchemaWithIntConstraint).field("foo").had(gt=1.0)))

    old_constraint = schemas["2000-01-01"][SchemaWithIntConstraint].model_json_schema()["properties"]["foo"]
    assert repr(old_constraint["exclusiveMinimum"]) == "1.0"
    new_constraint = schemas["2001-01-01"][SchemaWithIntConstraint].model_json_schema()["properties"]["foo"]
    assert repr(new_constraint["exclusiveMinimum"]) == "1"


@pytest.mark.parametrize(
    ("attributes", "new_attributes"),
    [
        pytest.param({"default": float("1.5")}, {"default": float("1.5")}, id="float"),
        pytest.param({"default": complex("1+2j")}, {"default": complex("1+2j")}, id="complex"),
        pytest.param({"default": Decimal("1.0")}, {"default": Decimal("1.0")}, id="decimal"),
        pytest.param({"default": nan}, {"default": nan}, id="the_same_nan"),
        pytest.param({"default": {"a": [1, (2, 3)]}}, {"default": {"a": [1, (2, 3)]}}, id="dict_with_list_and_tuple"),
        pytest.param({"default": frozenset({1, 2})}, {"default": frozenset({2, 1})}, id="frozenset"),
        pytest.param(
            {"validation_alias": AliasChoices("a", "b")},
            {"validation_alias": AliasChoices("a", "b")},
            id="dataclass",
        ),
    ],
)
def test__schema_field_had__attribute_that_is_the_same_as_the_current_one__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, attributes: dict[str, Any], new_attributes: dict[str, Any]
):
    class SchemaWithAttribute(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        foo: Any = Field(**attributes)

    with pytest.raises(InvalidGenerationInstructionError, match="but it already has that value"):
        create_runtime_schemas(version_change(schema(SchemaWithAttribute).field("foo").had(**new_attributes)))


@pytest.mark.parametrize(
    ("old_type", "new_type"),
    [
        pytest.param(Union[str, int], Union[int, str], id="union_in_another_order"),
        pytest.param(Literal["a", "b"], Literal["b", "a"], id="literal_in_another_order"),
        pytest.param(Literal[1], Literal[True], id="literal_with_int_to_literal_with_bool"),
    ],
)
def test__schema_field_had__type_that_equals_the_current_one_but_differs__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas, old_type: Any, new_type: Any
):
    class SchemaWithType(BaseModel):
        foo: old_type

    class SchemaWithNewType(BaseModel):
        foo: new_type

    schemas = create_runtime_schemas(version_change(schema(SchemaWithType).field("foo").had(type=new_type)))

    def field_schema(model: type[BaseModel]) -> str:
        return json.dumps(model.model_json_schema()["properties"]["foo"])

    assert field_schema(schemas["2000-01-01"][SchemaWithType]) == field_schema(SchemaWithNewType)
    assert field_schema(schemas["2001-01-01"][SchemaWithType]) == field_schema(SchemaWithType)
    assert field_schema(SchemaWithType) != field_schema(SchemaWithNewType)


@pytest.mark.parametrize(
    ("old_type", "new_type"),
    [
        pytest.param(list[int], list[int], id="generic_alias"),
        pytest.param(dict[str, list[int]], dict[str, list[int]], id="nested_generic_alias"),
    ],
)
def test__schema_field_had__type_that_is_the_same_as_the_current_one__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, old_type: Any, new_type: Any
):
    class SchemaWithType(BaseModel):
        foo: old_type

    with pytest.raises(InvalidGenerationInstructionError, match="but it already has type"):
        create_runtime_schemas(version_change(schema(SchemaWithType).field("foo").had(type=new_type)))


@dataclasses.dataclass(frozen=True, eq=False)
class SelfReferencingNode:
    parent: Any
    label: str


def _self_referencing_node(label: str) -> SelfReferencingNode:
    node = SelfReferencingNode(parent=None, label=label)
    object.__setattr__(node, "parent", node)
    return node


def test__schema_field_had__default_that_refers_to_itself__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithSelfReferencingDefault(BaseModel):
        foo: Annotated[Any, PlainSerializer(lambda node: node.label)] = _self_referencing_node("old")

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithSelfReferencingDefault).field("foo").had(default=_self_referencing_node("new")))
    )

    assert schemas["2000-01-01"][SchemaWithSelfReferencingDefault]().model_dump() == {"foo": "new"}
    assert schemas["2001-01-01"][SchemaWithSelfReferencingDefault]().model_dump() == {"foo": "old"}


def test__schema_field_had__default_that_refers_to_itself_and_is_the_same_object__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    node = _self_referencing_node("old")

    class SchemaWithSelfReferencingDefault(BaseModel):
        foo: Any = node

    with pytest.raises(InvalidGenerationInstructionError, match="but it already has that value"):
        create_runtime_schemas(version_change(schema(SchemaWithSelfReferencingDefault).field("foo").had(default=node)))


@pytest.mark.parametrize(
    "new_value",
    [
        pytest.param(set(range(19_999)) | {99_999}, id="another_set"),
        pytest.param(set(range(20_000)), id="the_same_set"),
    ],
)
def test__schema_field_had__default_that_is_a_large_set__should_compare_it_quickly(
    create_runtime_schemas: CreateRuntimeSchemas, new_value: set[int]
):
    class SchemaWithLargeSetDefault(BaseModel):
        foo: Any = set(range(20_000))

    started_at = time.perf_counter()
    with contextlib.suppress(InvalidGenerationInstructionError):
        create_runtime_schemas(version_change(schema(SchemaWithLargeSetDefault).field("foo").had(default=new_value)))

    # Comparing every item with every other item takes minutes
    assert time.perf_counter() - started_at < 5


class TaggedList(list[int]):
    tag: str


class TaggedTuple(tuple[int, ...]):  # noqa: SLOT001 # It needs __dict__ to hold the tag
    tag: str


class TaggedDict(dict[str, int]):
    tag: str


class TaggedSet(set[int]):
    tag: str


class TaggedFrozenset(frozenset[int]):
    tag: str


@dataclasses.dataclass
class Box:
    value: int


class WithTagSlot:
    __slots__ = "tag"


@dataclasses.dataclass
class BoxWithInheritedSlot(WithTagSlot):
    value: int


@dataclasses.dataclass
class BoxWithExtraSlot:
    __slots__ = ("tag", "value")
    value: int


def _tagged(value: Any, tag: str) -> Any:
    value.tag = tag
    return value


@pytest.mark.parametrize(
    "make_value",
    [
        pytest.param(lambda: TaggedList([1]), id="list_subclass"),
        pytest.param(lambda: TaggedTuple((1,)), id="tuple_subclass"),
        pytest.param(lambda: TaggedDict({"a": 1}), id="dict_subclass"),
    ],
)
def test__schema_field_had__default_of_container_subclass_with_state_that_equality_ignores__should_not_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas, make_value: Callable[[], Any]
):
    # The generated versions don't keep the state of these defaults, so we can only check that Cadwyn accepts them
    class SchemaWithTaggedDefault(BaseModel):
        foo: Any = _tagged(make_value(), "old")

    create_runtime_schemas(
        version_change(schema(SchemaWithTaggedDefault).field("foo").had(default=_tagged(make_value(), "new")))
    )


@pytest.mark.parametrize(
    "make_value",
    [
        pytest.param(lambda: TaggedSet({1}), id="set_subclass"),
        pytest.param(lambda: TaggedFrozenset({1}), id="frozenset_subclass"),
        pytest.param(lambda: Box(1), id="dataclass_with_attribute_that_is_not_a_field"),
        pytest.param(lambda: BoxWithExtraSlot(1), id="dataclass_with_slot_that_is_not_a_field"),
        pytest.param(lambda: BoxWithInheritedSlot(1), id="dataclass_with_base_that_is_not_a_dataclass"),
    ],
)
def test__schema_field_had__default_with_state_that_equality_ignores__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas, make_value: Callable[[], Any]
):
    class SchemaWithTaggedDefault(BaseModel):
        foo: Annotated[Any, PlainSerializer(lambda value: value.tag)] = _tagged(make_value(), "old")

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithTaggedDefault).field("foo").had(default=_tagged(make_value(), "new")))
    )

    assert schemas["2000-01-01"][SchemaWithTaggedDefault]().model_dump() == {"foo": "new"}
    assert schemas["2001-01-01"][SchemaWithTaggedDefault]().model_dump() == {"foo": "old"}


@dataclasses.dataclass(frozen=True, init=False)
class DataclassSet(set[int]):
    tag: str = "same tag"


def _dataclass_set(*items: int) -> DataclassSet:
    value = DataclassSet()
    value.update(items)
    return value


def test__schema_field_had__default_of_dataclass_that_subclasses_set_with_other_items__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithDataclassSetDefault(BaseModel):
        foo: Annotated[Any, PlainSerializer(sorted)] = _dataclass_set(1)

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithDataclassSetDefault).field("foo").had(default=_dataclass_set(2)))
    )

    assert schemas["2000-01-01"][SchemaWithDataclassSetDefault]().model_dump() == {"foo": [2]}
    assert schemas["2001-01-01"][SchemaWithDataclassSetDefault]().model_dump() == {"foo": [1]}


class CantBeCompared:
    def __init__(self, label: str) -> None:
        self.label = label

    @override
    def __hash__(self) -> int:
        return 1

    @override
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("Can't compare")  # pragma: no cover


def test__schema_field_had__default_of_set_with_items_that_cant_be_compared__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class SchemaWithSetDefault(BaseModel):
        foo: Annotated[Any, PlainSerializer(lambda value: sorted(item.label for item in value))] = {
            CantBeCompared("old")
        }

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithSetDefault).field("foo").had(default={CantBeCompared("new")}))
    )

    assert schemas["2000-01-01"][SchemaWithSetDefault]().model_dump() == {"foo": ["new"]}
    assert schemas["2001-01-01"][SchemaWithSetDefault]().model_dump() == {"foo": ["old"]}


@dataclasses.dataclass
class ParentDataclass:
    x: int


@dataclasses.dataclass(eq=False)
class ChildDataclassWithoutEq(ParentDataclass):
    y: int

    def __deepcopy__(self, memo: dict[int, Any]) -> "ChildDataclassWithoutEq":
        # So the tests can tell the copies of the defaults apart
        return self


def test__schema_field_had__default_of_dataclass_that_turns_off_eq_of_its_parent__should_change_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    old_value, new_value = ChildDataclassWithoutEq(1, 2), ChildDataclassWithoutEq(1, 2)

    class SchemaWithDataclassDefault(BaseModel):
        foo: Any = old_value

    schemas = create_runtime_schemas(
        version_change(schema(SchemaWithDataclassDefault).field("foo").had(default=new_value))
    )

    assert schemas["2000-01-01"][SchemaWithDataclassDefault].model_fields["foo"].default is new_value
    assert schemas["2001-01-01"][SchemaWithDataclassDefault].model_fields["foo"].default is old_value


def _return_self(self: Any, memo: dict[int, Any]) -> Any:
    return self


def _keep_when_copied(cls: type[Any]) -> type[Any]:
    """Make the copies of the instances the instances themselves, so the tests can tell the defaults apart"""
    cls.__deepcopy__ = _return_self
    return cls


def _values_with_module_that_is_a_str_subclass(calls: list[str]) -> tuple[Any, Any]:
    class ModuleName(str):
        __slots__ = ()

        @override
        def __hash__(self) -> int:
            calls.append("__hash__")  # pragma: no cover
            return str.__hash__(self)  # pragma: no cover

        @override
        def __eq__(self, other: object) -> bool:
            calls.append("__eq__")
            return str.__eq__(self, other)

    class WithModuleThatIsAStrSubclass:
        pass

    WithModuleThatIsAStrSubclass.__module__ = ModuleName("typing")
    _keep_when_copied(WithModuleThatIsAStrSubclass)
    return WithModuleThatIsAStrSubclass(), WithModuleThatIsAStrSubclass()


def _values_whose_metaclass_has_getattr(calls: list[str]) -> tuple[Any, Any]:
    class MetaclassWithGetattr(type):
        def __getattr__(cls, name: str) -> Any:
            calls.append(f"__getattr__({name})")
            raise AttributeError(name)

    class WithMetaclassThatHasGetattr(metaclass=MetaclassWithGetattr):
        pass

    _keep_when_copied(WithMetaclassThatHasGetattr)
    return WithMetaclassThatHasGetattr(), WithMetaclassThatHasGetattr()


def _values_with_class_property(calls: list[str]) -> tuple[Any, Any]:
    class WithClassProperty:
        __module__ = "typing"

        @property
        @override
        def __class__(self) -> type:
            calls.append("__class__")
            return object

    _keep_when_copied(WithClassProperty)
    return WithClassProperty(), WithClassProperty()


def _dataclass_values_whose_field_is_a_descriptor(calls: list[str]) -> tuple[Any, Any]:
    @dataclasses.dataclass
    class BoxWithFieldDescriptor:
        value: int

    _keep_when_copied(BoxWithFieldDescriptor)
    old_value, new_value = BoxWithFieldDescriptor(1), BoxWithFieldDescriptor(1)

    def read_value(box: BoxWithFieldDescriptor) -> int:
        calls.append("value")  # pragma: no cover
        return 1  # pragma: no cover

    # The instances keep their values in their __dict__, but reading the field runs the property
    BoxWithFieldDescriptor.value = property(read_value)  # ty: ignore[invalid-assignment]
    return old_value, new_value


@pytest.mark.parametrize(
    "make_values",
    [
        pytest.param(_values_with_module_that_is_a_str_subclass, id="module_that_is_a_str_subclass"),
        pytest.param(_values_whose_metaclass_has_getattr, id="metaclass_with_getattr"),
        pytest.param(_values_with_class_property, id="class_property"),
        pytest.param(_dataclass_values_whose_field_is_a_descriptor, id="dataclass_field_descriptor"),
    ],
)
def test__schema_field_had__default_that_could_run_user_code_when_inspected__should_change_it_without_running_it(
    create_runtime_schemas: CreateRuntimeSchemas, make_values: Callable[[list[str]], tuple[Any, Any]]
):
    calls: list[str] = []
    old_value, new_value = make_values(calls)

    class SchemaWithDefault(BaseModel):
        foo: Any = old_value

    # Pydantic and Cadwyn run some of this code anyway when they build the models of the versions, e.g. isinstance()
    # reads __class__, so we count the calls when the instruction doesn't compare the default
    calls.clear()
    create_runtime_schemas(version_change(schema(SchemaWithDefault).field("foo").had(description="Foo")))
    calls_without_comparing_the_default = Counter(calls)
    calls.clear()
    schemas = create_runtime_schemas(version_change(schema(SchemaWithDefault).field("foo").had(default=new_value)))

    assert Counter(calls) - calls_without_comparing_the_default == Counter()
    assert schemas["2000-01-01"][SchemaWithDefault].model_fields["foo"].default is new_value
    assert schemas["2001-01-01"][SchemaWithDefault].model_fields["foo"].default is old_value


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
