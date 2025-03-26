from __future__ import annotations

import re
from typing import Annotated

import pytest
from pydantic import BaseModel, WithJsonSchema
from typing_extensions import TypeAlias

from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import schema
from tests.conftest import CreateRuntimeSchemas, assert_models_are_equal, version_change

Foo: TypeAlias = int


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
