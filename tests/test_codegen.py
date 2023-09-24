import importlib
import inspect
import json
import re
from contextvars import ContextVar
from datetime import date
from enum import Enum, auto
from pathlib import Path
from types import ModuleType
from typing import Any, Union

import pytest
from pydantic import BaseModel, Field

from tests._data.unversioned_schema_dir import UnversionedSchema2
from tests._data.unversioned_schemas import UnversionedSchema3
from tests.conftest import CreateSimpleVersionedSchemas, CreateVersionedSchemas, version_change
from universi import regenerate_dir_to_all_versions
from universi.exceptions import (
    CodeGenerationError,
    InvalidGenerationInstructionError,
)
from universi.structure import (
    Version,
    VersionBundle,
    VersionChange,
    enum,
    schema,
)
from universi.structure.data import RequestInfo, convert_request_to_next_version_for


def serialize(enum: type[Enum]) -> dict[str, Any]:
    return {member.name: member.value for member in enum}


def assert_field_had_changes_apply(
    model: type[BaseModel],
    attr: str,
    attr_value: Any,
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    # No actual need to check 2001 because it's just star imports
    v2000_01_01, _ = create_simple_versioned_schemas(
        schema(getattr(latest_module, model.__name__)).field("foo").had(**{attr: attr_value}),
    )

    # For some reason it said that auto and Field were not defined, even though I was importing them
    globals_2000 = {"auto": auto, "Field": Field, "__name__": v2000_01_01.__name__}
    # Otherwise, when re-importing and rewriting the same files many times, at some point python just starts
    # putting the module into a hardcore cache that cannot be updated by removing entry from sys.modules or
    # using importlib.reload -- only by waiting around 1.5 seconds in-between tests.
    exec(inspect.getsource(v2000_01_01), globals_2000, globals_2000)
    assert getattr(globals_2000[model.__name__].__fields__["foo"].field_info, attr) == attr_value


def test__latest_enums_are_unchanged(latest_module: ModuleType):
    """If it is changed -- all tests will break

    So this test is essentially a validation for other tests.
    """

    assert serialize(latest_module.EmptyEnum) == {}
    assert serialize(latest_module.EnumWithOneMember) == {"a": 1}
    assert serialize(latest_module.EnumWithTwoMembers) == {"a": 1, "b": 2}


def test__enum_had__original_enum_is_empty(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        enum(latest_module.EmptyEnum).had(b=auto()),
    )

    assert serialize(v2000_01_01.EmptyEnum) == {"b": 1}
    assert serialize(v2001_01_01.EmptyEnum) == serialize(latest_module.EmptyEnum)


def test__enum_had__original_enum_is_nonempty(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        enum(latest_module.EnumWithOneMember).had(b=7),
    )

    assert serialize(v2000_01_01.EnumWithOneMember) == {"a": 1, "b": 7}
    assert serialize(v2001_01_01.EnumWithOneMember) == serialize(latest_module.EnumWithOneMember)


def test__enum_didnt_have__original_enum_has_one_member(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        enum(latest_module.EnumWithOneMember).didnt_have("a"),
    )

    assert serialize(v2000_01_01.EnumWithOneMember) == {}
    assert serialize(latest_module.EnumWithOneMember) == serialize(v2001_01_01.EnumWithOneMember)


def test__enum_didnt_have__original_enum_has_two_members(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        enum(latest_module.EnumWithTwoMembers).didnt_have("a"),
    )

    assert serialize(v2000_01_01.EnumWithTwoMembers) == {"b": 2}
    assert serialize(latest_module.EnumWithTwoMembers) == serialize(v2001_01_01.EnumWithTwoMembers)


def test__enum_had__original_schema_is_empty(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        enum(latest_module.EmptyEnum).had(b=7),
    )

    assert serialize(v2000_01_01.EmptyEnum) == {"b": 7}
    assert serialize(v2001_01_01.EmptyEnum) == serialize(latest_module.EmptyEnum)


def test__field_existed_as__original_schema_is_empty(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.EmptySchema).field("bar").existed_as(type=int, info=Field(description="hewwo")),
    )
    assert len(v2001_01_01.EmptySchema.__fields__) == 0

    assert (
        inspect.getsource(v2000_01_01.EmptySchema)
        == "class EmptySchema(BaseModel):\n    bar: int = Field(description='hewwo')\n"
    )


def test__field_existed_as__original_schema_has_a_field(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.SchemaWithOneStrField).field("bar").existed_as(type=int, info=Field(description="hewwo")),
    )

    assert inspect.getsource(v2000_01_01.SchemaWithOneStrField) == (
        "class SchemaWithOneStrField(BaseModel):\n"
        "    foo: str = Field(default='foo')\n"
        "    bar: int = Field(description='hewwo')\n"
    )

    assert (
        inspect.getsource(v2001_01_01.SchemaWithOneStrField)
        == 'class SchemaWithOneStrField(BaseModel):\n    foo: str = Field(default="foo")\n'
    )


def test__field_existed_as__extras_are_added__should_generate_properly(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.SchemaWithExtras).field("bar").existed_as(type=int, info=Field(deflolt="hewwo")),
    )

    assert inspect.getsource(v2000_01_01.SchemaWithExtras) == (
        "class SchemaWithExtras(BaseModel):\n"
        "    foo: str = Field(lulz='foo')\n"
        "    bar: int = Field(deflolt='hewwo')\n"
    )
    assert (
        inspect.getsource(v2001_01_01.SchemaWithExtras)
        == 'class SchemaWithExtras(BaseModel):\n    foo: str = Field(lulz="foo")\n'
    )


def test__schema_field_didnt_exist(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.SchemaWithOneStrField).field("foo").didnt_exist,
    )

    assert inspect.getsource(v2000_01_01.SchemaWithOneStrField) == "class SchemaWithOneStrField(BaseModel):\n    pass\n"

    assert (
        inspect.getsource(v2001_01_01.SchemaWithOneStrField)
        == 'class SchemaWithOneStrField(BaseModel):\n    foo: str = Field(default="foo")\n'
    )


def test__schema_field_didnt_exist__field_is_missing__should_raise_error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a field "bar" from "SchemaWithOneStrField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneStrField).field("bar").didnt_exist,
        )


def test__schema_field_didnt_exist__field_is_private(
    create_versioned_schemas: CreateVersionedSchemas,
    latest_module: ModuleType,
):
    v2000, v2001 = create_versioned_schemas(
        version_change(
            schema(latest_module.SchemaWithPrivateAttrs).field("_non_fillable_attr").didnt_exist,
        ),
    )

    assert inspect.getsource(v2000.SchemaWithPrivateAttrs) == (
        "class SchemaWithPrivateAttrs(FillablePrivateAttrMixin, BaseModel):\n"
        "    _fillable_attr: str = FillablePrivateAttr()\n"
    )

    assert inspect.getsource(v2001.SchemaWithPrivateAttrs) == (
        "class SchemaWithPrivateAttrs(FillablePrivateAttrMixin, BaseModel):\n"
        '    _non_fillable_attr: str = PrivateAttr(default="hewwo")\n'
        "    _fillable_attr: str = FillablePrivateAttr()\n"
    )


def test__schema_field_didnt_exist__field_is_fillable_private(
    create_versioned_schemas: CreateVersionedSchemas,
    latest_module: ModuleType,
):
    v2000, v2001 = create_versioned_schemas(
        version_change(
            schema(latest_module.SchemaWithPrivateAttrs).field("_fillable_attr").didnt_exist,
        ),
    )

    assert inspect.getsource(v2000.SchemaWithPrivateAttrs) == (
        "class SchemaWithPrivateAttrs(FillablePrivateAttrMixin, BaseModel):\n"
        "    _non_fillable_attr: str = PrivateAttr(default='hewwo')\n"
    )

    assert inspect.getsource(v2001.SchemaWithPrivateAttrs) == (
        "class SchemaWithPrivateAttrs(FillablePrivateAttrMixin, BaseModel):\n"
        '    _non_fillable_attr: str = PrivateAttr(default="hewwo")\n'
        "    _fillable_attr: str = FillablePrivateAttr()\n"
    )


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
def test__field_had__int_field(
    attr: str,
    attr_value: Any,
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    """This test is here to guarantee that we can handle all parameter types we provide"""
    assert_field_had_changes_apply(
        latest_module.SchemaWithOneIntField,
        attr,
        attr_value,
        create_simple_versioned_schemas,
        latest_module,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("min_length", 20),
        ("max_length", 50),
        ("regex", r"hewwo darkness"),
    ],
)
def test__field_had__str_field(
    attr: str,
    attr_value: Any,
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    assert_field_had_changes_apply(
        latest_module.SchemaWithOneStrField,
        attr,
        attr_value,
        create_simple_versioned_schemas,
        latest_module,
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("max_digits", 12),
        ("decimal_places", 15),
    ],
)
def test__field_had__decimal_field(
    attr: str,
    attr_value: Any,
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    assert_field_had_changes_apply(
        latest_module.SchemaWithOneDecimalField,
        attr,
        attr_value,
        create_simple_versioned_schemas,
        latest_module,
    )


# TODO: https://github.com/Ovsyanka83/universi/issues/3
def test__field_had__constrained_field(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000, v2001 = create_simple_versioned_schemas(
        schema(latest_module.SchemaWithConstrainedInt).field("foo").had(alias="bar"),
    )

    assert (
        inspect.getsource(v2000.SchemaWithConstrainedInt)
        == "class SchemaWithConstrainedInt(BaseModel):\n    foo: conint(strict=False, lt=10) = Field(alias='bar')\n"
    )

    assert inspect.getsource(v2001.SchemaWithConstrainedInt) == (
        "class SchemaWithConstrainedInt(BaseModel):\n"
        "    foo: conint(lt=CONINT_LT_ALIAS)  # pyright: ignore[reportGeneralTypeIssues]\n"
    )


def test__field_had__default_factory(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(  # pragma: no cover
        schema(latest_module.SchemaWithOneIntField).field("foo").had(default_factory=lambda: 91),
    )

    assert v2000_01_01.SchemaWithOneIntField.__fields__["foo"].default_factory() == 91
    assert (
        v2001_01_01.SchemaWithOneIntField.__fields__["foo"].default_factory
        is latest_module.SchemaWithOneIntField.__fields__["foo"].default_factory
    )


def test__field_had__type(create_simple_versioned_schemas: CreateSimpleVersionedSchemas, latest_module: ModuleType):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.SchemaWithOneIntField).field("foo").had(type=bytes),
    )

    assert v2000_01_01.SchemaWithOneIntField.__fields__["foo"].annotation is bytes
    assert (
        v2001_01_01.SchemaWithOneIntField.__fields__["foo"].annotation
        is latest_module.SchemaWithOneIntField.__fields__["foo"].annotation
    )


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("exclude", [16, 17, 18]),
        ("include", [19, 20, 21]),
        ("min_items", 10),
        ("max_items", 15),
        ("unique_items", True),
    ],
)
def test__field_had__list_of_int_field(
    attr: str,
    attr_value: Any,
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    assert_field_had_changes_apply(
        latest_module.SchemaWithOneListOfIntField,
        attr,
        attr_value,
        create_simple_versioned_schemas,
        latest_module,
    )


def test__field_had__float_field(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    assert_field_had_changes_apply(
        latest_module.SchemaWithOneFloatField,
        "allow_inf_nan",
        attr_value=False,
        create_simple_versioned_schemas=create_simple_versioned_schemas,
        latest_module=latest_module,
    )


def test__schema_field_had__change_to_the_same_field_type__should_raise_error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "foo" to "<class \'int\'>" from'
            ' "SchemaWithOneIntField" in "MyVersionChange" but it already has type "<class \'int\'>"',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneIntField).field("foo").had(type=int),
        )


def test__schema_field_had__change_attr_to_same_value__should_raise_error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the attribute "default" of field "foo" from "SchemaWithOneStrField" to \'foo\' '
            'in "MyVersionChange" but it already has that value.',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneStrField).field("foo").had(default="foo"),
        )


def test__schema_field_had__nonexistent_field__should_raise_error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "boo" from "SchemaWithOneIntField" in '
            '"MyVersionChange" but it doesn\'t have such a field.',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneIntField).field("boo").had(type=int),
        )


def test__schema_field_had__trying_to_change_private_attr__should_raise_error(
    create_versioned_schemas: CreateVersionedSchemas,
    latest_module: ModuleType,
):
    # with insert_pytest_raises():
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the type of field "_non_fillable_attr" from "SchemaWithPrivateAttrs" in '
            '"MyVersionChange" but it is a private attribute and private attributes cannot be edited.',
        ),
    ):
        create_versioned_schemas(
            version_change(
                schema(latest_module.SchemaWithPrivateAttrs).field("_non_fillable_attr").had(type=int),
            ),
        )


def test__enum_had__same_name_as_other_value__error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a member "a" to "EnumWithOneMember" in '
            '"MyVersionChange" but there is already a member with that name and value.',
        ),
    ):
        create_simple_versioned_schemas(enum(latest_module.EnumWithOneMember).had(a=1))


def test__enum_didnt_have__nonexisting_name__error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a member "foo" from "EmptyEnum" in '
            '"MyVersionChange" but it doesn\'t have such a member.',
        ),
    ):
        create_simple_versioned_schemas(enum(latest_module.EmptyEnum).didnt_have("foo"))


def test__with_deleted_source_file__error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_package_name: str,
    data_dir: Path,
    api_version_var: ContextVar[date | None],
):
    (data_dir / "latest/another_temp1").mkdir(exist_ok=True)
    (data_dir / "latest/another_temp1/hello.py").touch()
    wrong_latest_module = importlib.import_module(data_package_name + ".latest.another_temp1.hello")

    with pytest.raises(
        CodeGenerationError,
        match=f'Module "{wrong_latest_module}" is not a package',
    ):
        regenerate_dir_to_all_versions(
            wrong_latest_module,
            VersionBundle(
                Version(date(2000, 1, 1)),
                api_version_var=api_version_var,
            ),
        )


def test__non_python_files__copied_to_all_dirs(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_dir: Path,
):
    create_simple_versioned_schemas()
    assert json.loads(Path(data_dir / "v2000_01_01/json_files/foo.json").read_text()) == {"hello": "world"}
    assert json.loads(Path(data_dir / "v2001_01_01/json_files/foo.json").read_text()) == {"hello": "world"}


def test__non_pydantic_schema__error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_package_name,
):
    with pytest.raises(
        CodeGenerationError,
        match=re.escape(
            f"Model {latest_module.NonPydanticSchema} is not a subclass of BaseModel",
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.NonPydanticSchema).field("foo").didnt_exist,
        )


def test__schema_that_overrides_fields_from_mro(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.SchemaThatOverridesField).field("bar").existed_as(type=int),
    )

    assert (
        inspect.getsource(v2000_01_01.SchemaThatOverridesField)
        == "class SchemaThatOverridesField(SchemaWithOneIntField):\n    foo: bytes = Field()\n    bar: int = Field()\n"
    )

    assert (
        inspect.getsource(v2001_01_01.SchemaThatOverridesField)
        == "class SchemaThatOverridesField(SchemaWithOneIntField):\n    foo: bytes\n"
    )


def test__schema_existed_as(create_simple_versioned_schemas: CreateSimpleVersionedSchemas, latest_module: ModuleType):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.EmptySchema).field("bar").existed_as(type=int, info=Field(example=83)),
    )

    assert (
        inspect.getsource(v2000_01_01.EmptySchema)
        == "class EmptySchema(BaseModel):\n    bar: int = Field(example=83)\n"
    )

    assert inspect.getsource(v2001_01_01.EmptySchema) == "class EmptySchema(BaseModel):\n    pass\n"


def test__schema_field_existed_as__already_existing_field__should_raise_error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to add a field "foo" to "SchemaWithOneIntField" in '
            '"MyVersionChange" but there is already a field with that name.',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneIntField).field("foo").existed_as(type=str),
        )


def test__schema_defined_in_a_non_init_file(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_package_name: str,
):
    module = importlib.import_module(data_package_name + ".latest.some_schema")

    create_simple_versioned_schemas(schema(module.MySchema).field("foo").didnt_exist)

    v2000 = importlib.import_module(data_package_name + ".v2000_01_01.some_schema")
    v2001 = importlib.import_module(data_package_name + ".v2001_01_01.some_schema")

    assert inspect.getsource(v2000.MySchema) == "class MySchema(BaseModel):\n    pass\n"
    assert inspect.getsource(v2001.MySchema) == "class MySchema(BaseModel):\n    foo: int\n"


def test__with_weird_data_types(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_package_name: str,
):
    weird_schemas = importlib.import_module(data_package_name + ".latest.weird_schemas")
    create_simple_versioned_schemas(
        schema(weird_schemas.ModelWithWeirdFields).field("bad").existed_as(type=int),
    )

    v2000 = importlib.import_module(data_package_name + ".v2000_01_01.weird_schemas")
    v2001 = importlib.import_module(data_package_name + ".v2001_01_01.weird_schemas")

    assert inspect.getsource(v2000.ModelWithWeirdFields) == (
        "class ModelWithWeirdFields(BaseModel):\n"
        "    foo: dict = Field(default={'a': 'b'})\n"
        "    bar: list[int] = Field(default_factory=my_default_factory)\n"
        "    baz: typing.Literal[MyEnum.baz] = Field()\n"
        "    bad: int = Field()\n"
    )

    assert inspect.getsource(v2001.ModelWithWeirdFields) == (
        "class ModelWithWeirdFields(BaseModel):\n"
        '    foo: dict = Field(default={"a": "b"})\n'
        "    bar: list[int] = Field(default_factory=my_default_factory)\n"
        "    baz: Literal[MyEnum.baz]\n"
    )


def test__union_fields(create_simple_versioned_schemas: CreateSimpleVersionedSchemas, latest_module: ModuleType):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.SchemaWithUnionFields).field("baz").existed_as(type=int | latest_module.EmptySchema),
        schema(latest_module.SchemaWithUnionFields).field("daz").existed_as(type=Union[int, latest_module.EmptySchema]),
    )

    assert inspect.getsource(v2000_01_01.SchemaWithUnionFields) == (
        "class SchemaWithUnionFields(BaseModel):\n"
        "    foo: typing.Union[int, str] = Field()\n"
        "    bar: typing.Union[EmptySchema, None] = Field()\n"
        "    baz: typing.Union[int, EmptySchema] = Field()\n"
        "    daz: typing.Union[int, EmptySchema] = Field()\n"
    )
    assert inspect.getsource(v2001_01_01.SchemaWithUnionFields) == (
        "class SchemaWithUnionFields(BaseModel):\n    foo: int | str\n    bar: EmptySchema | None\n"
    )


def test__imports_and_aliases(create_simple_versioned_schemas: CreateSimpleVersionedSchemas, latest_module: ModuleType):
    v2000_01_01, v2001_01_01 = create_simple_versioned_schemas(
        schema(latest_module.EmptySchemaWithArbitraryTypesAllowed)
        .field("foo")
        .existed_as(type="Logger", import_from="logging", import_as="MyLogger"),
        schema(latest_module.EmptySchemaWithArbitraryTypesAllowed)
        .field("bar")
        .existed_as(
            type=UnversionedSchema3,
            import_from="..unversioned_schemas",
            import_as="MyLittleSchema",
        ),
        schema(latest_module.EmptySchemaWithArbitraryTypesAllowed)
        .field("baz")
        .existed_as(type=UnversionedSchema2, import_from="..unversioned_schema_dir"),
    )
    assert inspect.getsource(v2000_01_01.EmptySchemaWithArbitraryTypesAllowed) == (
        "class EmptySchemaWithArbitraryTypesAllowed(BaseModel, arbitrary_types_allowed=True):\n"
        "    foo: 'MyLogger' = Field()\n"
        "    bar: 'MyLittleSchema' = Field()\n"
        "    baz: UnversionedSchema2 = Field()\n"
    )
    assert inspect.getsource(v2001_01_01.EmptySchemaWithArbitraryTypesAllowed) == (
        "class EmptySchemaWithArbitraryTypesAllowed(BaseModel, arbitrary_types_allowed=True):\n    pass\n"
    )


def test__unions__init_file(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_package_name,
):
    create_simple_versioned_schemas()
    v2000, v2001 = (
        importlib.import_module(data_package_name + ".v2000_01_01"),
        importlib.import_module(data_package_name + ".v2001_01_01"),
    )
    unions = importlib.import_module(data_package_name + ".unions")

    assert (
        unions.EnumWithOneMember == v2000.EnumWithOneMember | v2001.EnumWithOneMember | latest_module.EnumWithOneMember
    )
    assert (
        unions.SchemaWithOneIntField
        == v2000.SchemaWithOneIntField | v2001.SchemaWithOneIntField | latest_module.SchemaWithOneIntField
    )


def test__unions__regular_file(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
    data_package_name: str,
):
    create_simple_versioned_schemas()
    latest = importlib.import_module(data_package_name + ".latest.some_schema")
    unions = importlib.import_module(data_package_name + ".unions.some_schema")
    v2000 = importlib.import_module(data_package_name + ".v2000_01_01.some_schema")

    assert unions.MySchema == latest.MySchema | v2000.MySchema


def test__property(api_version_var: ContextVar[date | None], latest_module: ModuleType, data_package_name: str):
    def baz_property(hewwo: Any):
        raise NotImplementedError

    class VersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = (
            schema(latest_module.SchemaWithOneFloatField).property("baz").was(baz_property),
        )

        @schema(latest_module.SchemaWithOneFloatField).property("bar").was
        def bar_property(arg1: list[str]):
            return 83

    class VersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = (
            schema(latest_module.SchemaWithOneFloatField).property("bar").didnt_exist,
        )

    assert VersionChange2.bar_property([]) == 83

    regenerate_dir_to_all_versions(
        latest_module,
        VersionBundle(
            Version(date(2002, 1, 1), VersionChange2),
            Version(date(2001, 1, 1), VersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
    )
    v2000_01_01, v2001_01_01, v2002_01_01 = (
        importlib.import_module(data_package_name + ".v2000_01_01"),
        importlib.import_module(data_package_name + ".v2001_01_01"),
        importlib.import_module(data_package_name + ".v2002_01_01"),
    )

    assert inspect.getsource(v2000_01_01.SchemaWithOneFloatField) == (
        "class SchemaWithOneFloatField(BaseModel):\n"
        "    foo: float = Field()\n\n"
        "    @property\n"
        "    def baz(hewwo):\n"
        "        raise NotImplementedError\n"
    )

    assert inspect.getsource(v2001_01_01.SchemaWithOneFloatField) == (
        "class SchemaWithOneFloatField(BaseModel):\n"
        "    foo: float = Field()\n\n"
        "    @property\n"
        "    def baz(hewwo):\n"
        "        raise NotImplementedError\n\n"
        "    @property\n"
        "    def bar(arg1):\n"
        "        return 83\n"
    )

    assert inspect.getsource(v2002_01_01.SchemaWithOneFloatField) == (
        "class SchemaWithOneFloatField(BaseModel):\n    foo: float\n"
    )


def test__delete_nonexistent_property(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to delete a property "bar" from "SchemaWithOneFloatField" in '
            '"MyVersionChange" but there is no such property defined in any of the migrations.',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneFloatField).property("bar").didnt_exist,
        )


def test__lambda_property(create_simple_versioned_schemas: CreateSimpleVersionedSchemas, latest_module: ModuleType):
    with pytest.raises(
        CodeGenerationError,
        match=re.escape(
            'Failed to migrate class "SchemaWithOneFloatField" to an older version because: '
            "You passed a lambda as a schema property. It is not supported yet. "
            "Please, use a regular function instead. The lambda you have passed: "
            'schema(latest_module.SchemaWithOneFloatField).property("bar").was(lambda _: "Hewwo"),  '
            "# pragma: no cover\n",
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneFloatField).property("bar").was(lambda _: "Hewwo"),  # pragma: no cover
        )


def test__property__there_is_already_field_with_the_same_name__error(
    create_simple_versioned_schemas: CreateSimpleVersionedSchemas,
    latest_module: ModuleType,
):
    def baz(hello: Any):
        raise NotImplementedError

    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to define a property "foo" inside "SchemaWithOneFloatField" in '
            '"MyVersionChange" but there is already a field with that name.',
        ),
    ):
        create_simple_versioned_schemas(
            schema(latest_module.SchemaWithOneFloatField).property("foo").was(baz),
        )


def test__schema_had_name__dependent_schema_is_not_altered(
    api_version_var: ContextVar[date | None],
    latest_module,
    data_package_name,
):
    class VersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            schema(latest_module.SchemaWithOneFloatField).had(name="MyFloatySchema"),
        ]

    class VersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            schema(latest_module.SchemaWithOneFloatField).had(name="MyFloatySchema2"),
        ]

    regenerate_dir_to_all_versions(
        latest_module,
        VersionBundle(
            Version(date(2002, 1, 1), VersionChange2),
            Version(date(2001, 1, 1), VersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
    )

    v2000_01_01, v2001_01_01, v2002_01_01 = (
        importlib.import_module(data_package_name + ".v2000_01_01"),
        importlib.import_module(data_package_name + ".v2001_01_01"),
        importlib.import_module(data_package_name + ".v2002_01_01"),
    )

    assert inspect.getsource(v2000_01_01.MyFloatySchema2) == (
        "class MyFloatySchema2(BaseModel):\n    foo: float = Field()\n"
    )
    assert inspect.getsource(v2001_01_01.MyFloatySchema) == (
        "class MyFloatySchema(BaseModel):\n    foo: float = Field()\n"
    )
    assert inspect.getsource(v2002_01_01.SchemaWithOneFloatField) == (
        "class SchemaWithOneFloatField(BaseModel):\n    foo: float\n"
    )
    assert inspect.getsource(v2000_01_01.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert inspect.getsource(v2001_01_01.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )
    assert inspect.getsource(v2002_01_01.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(SchemaWithOneFloatField):\n"
        "    foo: SchemaWithOneFloatField\n"
        "    bat: SchemaWithOneFloatField | int = Field(default=SchemaWithOneFloatField(foo=3.14))\n\n"
        "    def baz(self, daz: SchemaWithOneFloatField) -> SchemaWithOneFloatField:\n"
        "        return SchemaWithOneFloatField(foo=3.14)  # pragma: no cover\n"
    )

    some_schema_v2000 = importlib.import_module(data_package_name + ".v2000_01_01.some_schema")
    some_schema_v2001 = importlib.import_module(data_package_name + ".v2001_01_01.some_schema")
    some_schema_v2002 = importlib.import_module(data_package_name + ".v2002_01_01.some_schema")
    unions = importlib.import_module(data_package_name + ".unions")

    assert inspect.getsource(some_schema_v2000.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(BaseModel):\n    foo: MyFloatySchema2\n    bar: int\n"
    )

    assert inspect.getsource(some_schema_v2001.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(BaseModel):\n    foo: MyFloatySchema\n    bar: int\n"
    )

    assert inspect.getsource(some_schema_v2002.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(BaseModel):\n    foo: SchemaWithOneFloatField\n    bar: int\n"
    )

    assert str(unions.SchemaWithOneFloatField) == (
        f"{data_package_name}.latest.SchemaWithOneFloatField | "
        f"{data_package_name}.v2001_01_01.MyFloatySchema | "
        f"{data_package_name}.v2000_01_01.MyFloatySchema2"
    )


def test__schema_had_name__dependent_schema_is_altered(
    api_version_var: ContextVar[date | None],
    latest_module,
    data_package_name,
):
    some_schema = importlib.import_module(data_package_name + ".latest.some_schema")

    class VersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            schema(latest_module.SchemaWithOneFloatField).had(name="MyFloatySchema"),
            schema(latest_module.SchemaThatDependsOnAnotherSchema).field("gaz").existed_as(type=int),
            schema(some_schema.SchemaThatDependsOnAnotherSchema).field("bar").didnt_exist,
        ]

    class VersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            schema(latest_module.SchemaWithOneFloatField).had(name="MyFloatySchema2"),
            schema(latest_module.SchemaThatDependsOnAnotherSchema).field("gaz").didnt_exist,
        ]

    regenerate_dir_to_all_versions(
        latest_module,
        VersionBundle(
            Version(date(2002, 1, 1), VersionChange2),
            Version(date(2001, 1, 1), VersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
    )

    v2000_01_01, v2001_01_01, v2002_01_01 = (
        importlib.import_module(data_package_name + ".v2000_01_01"),
        importlib.import_module(data_package_name + ".v2001_01_01"),
        importlib.import_module(data_package_name + ".v2002_01_01"),
    )

    assert inspect.getsource(v2000_01_01.MyFloatySchema2) == (
        "class MyFloatySchema2(BaseModel):\n    foo: float = Field()\n"
    )
    assert inspect.getsource(v2001_01_01.MyFloatySchema) == (
        "class MyFloatySchema(BaseModel):\n    foo: float = Field()\n"
    )
    assert inspect.getsource(v2002_01_01.SchemaWithOneFloatField) == (
        "class SchemaWithOneFloatField(BaseModel):\n    foo: float\n"
    )
    assert (
        inspect.getsource(v2000_01_01.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2 = Field()\n"
        "    bat: typing.Union[MyFloatySchema2, int] = Field(default=MyFloatySchema2(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert (
        inspect.getsource(v2001_01_01.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema = Field()\n"
        "    bat: typing.Union[MyFloatySchema, int] = Field(default=MyFloatySchema(foo=3.14))\n"
        "    gaz: int = Field()\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )
    assert (
        inspect.getsource(v2002_01_01.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(SchemaWithOneFloatField):\n"
        "    foo: SchemaWithOneFloatField\n"
        "    bat: SchemaWithOneFloatField | int = Field(default=SchemaWithOneFloatField(foo=3.14))\n\n"
        "    def baz(self, daz: SchemaWithOneFloatField) -> SchemaWithOneFloatField:\n"
        "        return SchemaWithOneFloatField(foo=3.14)  # pragma: no cover\n"
    )
    some_schema_v2000 = importlib.import_module(data_package_name + ".v2000_01_01.some_schema")
    some_schema_v2001 = importlib.import_module(data_package_name + ".v2001_01_01.some_schema")
    some_schema_v2002 = importlib.import_module(data_package_name + ".v2002_01_01.some_schema")
    unions = importlib.import_module(data_package_name + ".unions")

    assert inspect.getsource(some_schema_v2000.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(BaseModel):\n    foo: MyFloatySchema2 = Field()\n"
    )

    assert inspect.getsource(some_schema_v2001.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(BaseModel):\n    foo: MyFloatySchema = Field()\n"
    )

    assert inspect.getsource(some_schema_v2002.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(BaseModel):\n    foo: SchemaWithOneFloatField\n    bar: int\n"
    )

    assert str(unions.SchemaWithOneFloatField) == (
        f"{data_package_name}.latest.SchemaWithOneFloatField | "
        f"{data_package_name}.v2001_01_01.MyFloatySchema | "
        f"{data_package_name}.v2000_01_01.MyFloatySchema2"
    )


def test__schema_had_name__trying_to_assign_to_the_same_name__should_raise_error(
    create_versioned_schemas: CreateVersionedSchemas,
    latest_module: ModuleType,
):
    # with insert_pytest_raises():
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of "EmptySchema" in "MyVersionChange" '
            "but it already has the name you tried to assign.",
        ),
    ):
        create_versioned_schemas(
            version_change(
                schema(latest_module.EmptySchema).had(name="EmptySchema"),  # pyright: ignore[reportGeneralTypeIssues]
            ),
        )


def test__union_generation__convert_request_to_next_version_for_one_schema__one_schema_is_skipped(
    api_version_var: ContextVar[date | None],
    data_package_name: str,
    latest_module: ModuleType,
):
    class VersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = []

        @convert_request_to_next_version_for(latest_module.SchemaWithOneIntField)
        def migrate(request: RequestInfo) -> None:
            raise NotImplementedError

    class VersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = []

    regenerate_dir_to_all_versions(
        latest_module,
        VersionBundle(
            Version(date(2002, 1, 1), VersionChange2),
            Version(date(2001, 1, 1), VersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
    )

    v2000 = importlib.import_module(data_package_name + ".v2000_01_01")
    unions = importlib.import_module(data_package_name + ".unions")

    assert unions.SchemaWithOneIntField == latest_module.SchemaWithOneIntField | v2000.SchemaWithOneIntField


def test__union_generation__convert_request_to_next_version_for_all_schemas__all_schemas_are_skipped(
    api_version_var: ContextVar[date | None],
    latest_module: ModuleType,
    data_package_name: str,
):
    class VersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = []

        @convert_request_to_next_version_for(latest_module.SchemaWithOneIntField)
        def migrate(request: RequestInfo) -> None:
            raise NotImplementedError

    class VersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = []

        @convert_request_to_next_version_for(latest_module.SchemaWithOneIntField)
        def migrate(request: RequestInfo) -> None:
            raise NotImplementedError

    regenerate_dir_to_all_versions(
        latest_module,
        VersionBundle(
            Version(date(2002, 1, 1), VersionChange2),
            Version(date(2001, 1, 1), VersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
    )

    unions = importlib.import_module(data_package_name + ".unions")

    assert unions.SchemaWithOneIntField == latest_module.SchemaWithOneIntField
