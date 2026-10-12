from __future__ import annotations

import re
from typing import Annotated, Generic, TypeAlias, TypeVar

import pytest
from pydantic import BaseModel, WithJsonSchema

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
