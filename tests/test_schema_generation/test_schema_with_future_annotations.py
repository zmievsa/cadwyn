from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import Annotated, Any, ClassVar, Final, Generic, Optional, TypeAlias, TypeGuard, TypeVar

import pytest
from pydantic import BaseModel, WithJsonSchema
from pydantic.warnings import PydanticDeprecatedSince211

from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import schema
from tests.conftest import CreateRuntimeSchemas, assert_models_are_equal, version_change

Foo: TypeAlias = int
_T = TypeVar("_T")


class MySchema(BaseModel):
    foo: Foo
    bar: Annotated[str, WithJsonSchema({"type": "string", "description": "Hi"})]


def test__schema_had_name(create_runtime_schemas: CreateRuntimeSchemas):
    schemas = create_runtime_schemas(version_change(schema(MySchema).had(name="Aww")))

    assert_models_are_equal(schemas["2000-01-01"][MySchema], MySchema)
    assert schemas["2000-01-01"][MySchema].__name__ == "Aww"
    assert schemas["2000-01-01"][MySchema].__qualname__ == "Aww"


def test__schema_had_name__with_the_same_name__should_raise_error(create_runtime_schemas: CreateRuntimeSchemas):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of "MySchema" in "MyVersionChange" '
            "but it already has the name you tried to assign."
        ),
    ):
        create_runtime_schemas(version_change(schema(MySchema).had(name="MySchema")))


class RecursiveItem(BaseModel):
    children: list[RecursiveItem] = []


class GenericParentWithRecursiveItem(BaseModel, Generic[_T]):
    value: _T
    bar: int = 0


class ChildWithRecursiveItem(GenericParentWithRecursiveItem[RecursiveItem]):
    pass


class OtherChildWithRecursiveItem(GenericParentWithRecursiveItem[RecursiveItem]):
    pass


class DiamondWithRecursiveItem(ChildWithRecursiveItem, OtherChildWithRecursiveItem):
    pass


def test__schema_field_had__recursive_type_arg_field_of_other_parent_of_diamond__should_generate_diamond(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(GenericParentWithRecursiveItem).field("bar").had(description="Bar"),
            schema(OtherChildWithRecursiveItem).field("value").had(description="Value"),
        )
    )

    diamond = schemas["2000-01-01"][DiamondWithRecursiveItem]
    assert diamond.model_validate({"value": {"children": [{}]}}).model_dump() == {
        "value": {"children": [{"children": []}]},
        "bar": 0,
    }


_ClassVarAlias = ClassVar


class ParentWithFooAsQuotedClassVar(BaseModel):
    foo: ClassVar[int]


class ChildThatDeclaresFooHiddenByQuotedClassVar(ParentWithFooAsQuotedClassVar):
    foo: str  # ty: ignore[invalid-attribute-override]


class ParentWithFooAsAliasedClassVar(BaseModel):
    foo: _ClassVarAlias[int]


class ChildThatDeclaresFooHiddenByAliasedClassVar(ParentWithFooAsAliasedClassVar):
    foo: str  # ty: ignore[invalid-attribute-override]


# Pydantic warns that it will treat such a name as a field in V3, and that the child's field shadows the class var
with warnings.catch_warnings():
    warnings.simplefilter("ignore", PydanticDeprecatedSince211)
    warnings.filterwarnings("ignore", 'Field name "foo" in "ChildThatDeclaresFooOverFinal" shadows', UserWarning)

    class ParentWithFinalFoo(BaseModel):
        foo: Final[int] = 1

    class ChildThatDeclaresFooOverFinal(ParentWithFinalFoo):
        foo: str  # ty: ignore[override-of-final-variable]


@pytest.mark.parametrize(
    "child",
    [
        pytest.param(ChildThatDeclaresFooHiddenByQuotedClassVar, id="quoted_class_var"),
        pytest.param(ChildThatDeclaresFooHiddenByAliasedClassVar, id="aliased_class_var"),
        pytest.param(
            ChildThatDeclaresFooOverFinal,
            id="final_with_default",
            # The copies of the models get the same warnings as the models
            marks=[
                pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince211"),
                pytest.mark.filterwarnings('ignore:Field name "foo" in "ChildThatDeclaresFooOverFinal" shadows'),
            ],
        ),
    ],
)
def test__schema_field_had_name__parent_declares_field_name_as_class_var__should_rename_field_in_child(
    create_runtime_schemas: CreateRuntimeSchemas,
    child: type[BaseModel],
):
    schemas = create_runtime_schemas(version_change(schema(child).field("foo").had(name="old_foo")))

    copied_child = schemas["2000-01-01"][child]
    assert list(copied_child.model_fields) == ["old_foo"]
    assert copied_child.model_validate({"old_foo": "v"}).model_dump() == {"old_foo": "v"}


class ItemOfModelWithSpecialForms(BaseModel):
    name: str


with warnings.catch_warnings():
    # Pydantic treats `Final` with a default as a class variable, and warns that it will become a field in pydantic v3
    warnings.filterwarnings("ignore", "Annotation .* is marked as final and has a default value")

    class ModelWithSpecialForms(BaseModel):
        constant: Final[int] = 1
        default_item: ClassVar[Optional[ItemOfModelWithSpecialForms]] = None
        item: Final[ItemOfModelWithSpecialForms]
        checker: Callable[[Any], TypeGuard[ItemOfModelWithSpecialForms]]

    class ChildOfModelWithSpecialForms(ModelWithSpecialForms):
        other: str


@pytest.mark.filterwarnings("ignore:Annotation .* is marked as final and has a default value")
def test__schema_field_had__child_of_model_with_special_forms__should_keep_class_vars_and_version_fields(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ItemOfModelWithSpecialForms).field("name").had(type=int),
            schema(ChildOfModelWithSpecialForms).field("other").had(description="Other"),
        )
    )

    model_copy = schemas["2000-01-01"][ChildOfModelWithSpecialForms]
    item_copy = schemas["2000-01-01"][ItemOfModelWithSpecialForms]
    assert model_copy.__class_vars__ == {"constant", "default_item"}
    assert model_copy.constant == 1
    assert set(model_copy.model_fields) == {"item", "checker", "other"}
    assert model_copy.model_fields["item"].annotation is item_copy
    assert model_copy.model_fields["item"].frozen is True
    assert model_copy.model_fields["checker"].annotation == Callable[[Any], TypeGuard[item_copy]]  # ty: ignore[invalid-type-form]
