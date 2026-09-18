import re
import typing
from enum import Enum
from typing import ClassVar, get_args, get_type_hints

import pytest
from pydantic import BaseModel
from typing_extensions import TypeAliasType

from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import schema
from tests.conftest import CreateRuntimeSchemas, assert_models_are_equal, version_change


class MySchema(BaseModel):
    foo: str


def test__generate_versioned_models__unaffected_models_and_enums__should_preserve_original_classes(
    create_runtime_schemas: CreateRuntimeSchemas,
) -> None:
    class Role(Enum):
        admin = "admin"

    role_list = TypeAliasType("role_list", list[Role])

    class User(BaseModel):
        roles: role_list
        callbacks: ClassVar[typing.Dict[str, typing.Callable[[str], str]]] = {}  # noqa: UP006  # Preserve legacy annotations.

    class Admin(User):
        name: str

    schemas = create_runtime_schemas(version_change(schema(MySchema).field("foo").had(name="bar")))

    for generator in schemas.values():
        assert generator[Role] is Role
        assert generator[User] is User
        assert generator[Admin] is Admin


def test__generate_versioned_models__dependent_models__should_migrate_bases_and_nested_schemas(
    create_runtime_schemas: CreateRuntimeSchemas,
) -> None:
    class Child(MySchema):
        pass

    item_list = TypeAliasType("item_list", list[MySchema])

    class Container(BaseModel):
        items: item_list
        example: ClassVar[MySchema] = MySchema(foo="value")

    schemas = create_runtime_schemas(version_change(schema(MySchema).field("foo").had(name="bar")))

    for generator in schemas.values():
        assert get_args(get_type_hints(generator[Container])["example"]) == (generator[MySchema],)
    assert schemas["2000-01-01"][Child].model_validate({"bar": "value"}).model_dump() == {"bar": "value"}
    assert schemas["2001-01-01"][Child].model_validate({"foo": "value"}).model_dump() == {"foo": "value"}
    assert schemas["2000-01-01"][Container].model_validate({"items": [{"bar": "value"}]}).model_dump() == {
        "items": [{"bar": "value"}]
    }
    assert schemas["2001-01-01"][Container].model_validate({"items": [{"foo": "value"}]}).model_dump() == {
        "items": [{"foo": "value"}]
    }


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
