import re
from typing import ClassVar

import pytest
from pydantic import BaseModel

from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import schema
from tests.conftest import CreateRuntimeSchemas, assert_models_are_equal, version_change


class MySchema(BaseModel):
    foo: str


def test__generate_versioned_models__table_models__should_preserve_original_classes(
    create_runtime_schemas: CreateRuntimeSchemas,
) -> None:
    class UserFields(BaseModel):
        name: str

    class User(UserFields):
        __table__: ClassVar[object] = object()

    class Admin(User):
        role: str

    schemas = create_runtime_schemas(version_change(schema(UserFields).field("name").had(name="username")))

    for generator in schemas.values():
        assert generator[User] is User
        assert generator[Admin] is Admin
    assert set(schemas["2000-01-01"][UserFields].model_fields) == {"username"}
    assert set(schemas["2001-01-01"][UserFields].model_fields) == {"name"}


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
