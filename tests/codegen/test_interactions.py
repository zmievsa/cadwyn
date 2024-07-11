import importlib
import inspect
import json
import re
import textwrap
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from cadwyn.codegen._main import generate_code_for_versioned_packages
from cadwyn.exceptions import (
    CodeGenerationError,
    InvalidGenerationInstructionError,
)
from cadwyn.structure import (
    Version,
    VersionBundle,
    schema,
)
from tests.conftest import (
    CreateLocalSimpleVersionedPackages,
    CreateLocalVersionedPackages,
    CreateVersionedPackages,
    HeadModuleFor,
    _FakeModuleWithEmptyClasses,
    version_change,
)


def test__with_file_instead_of_package__error(
    head_package_path: str,
    head_dir: Path,
    api_version_var: ContextVar[date | None],
):
    (head_dir / "hello.py").touch()
    importlib.invalidate_caches()
    wrong_latest_module = importlib.import_module(head_package_path + ".hello")

    # with insert_pytest_raises():
    with pytest.raises(
        CodeGenerationError,
        match=re.escape(f'"{wrong_latest_module}" is not a package'),
    ):
        generate_code_for_versioned_packages(
            wrong_latest_module,
            VersionBundle(
                Version(date(2000, 1, 1)),
                api_version_var=api_version_var,
            ),
        )


def test__non_python_files__copied_to_all_dirs(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_with_empty_classes: _FakeModuleWithEmptyClasses,
    head_dir: Path,
    temp_data_dir: Path,
):
    json_file_dir = head_dir / "json_files"
    json_file_dir.mkdir()
    json_file_dir.joinpath("foo.json").write_text('{"hello":"world"}')

    create_local_simple_versioned_packages()
    assert json.loads(Path(temp_data_dir / "v2000_01_01/json_files/foo.json").read_text()) == {"hello": "world"}
    assert json.loads(Path(temp_data_dir / "v2001_01_01/json_files/foo.json").read_text()) == {"hello": "world"}


def test__non_pydantic_schema__error(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_module_for: HeadModuleFor,
):
    latest = head_module_for(
        """
    class NonPydanticSchema:
        foo: str
    """,
    )
    with pytest.raises(
        CodeGenerationError,
        match=re.escape(
            f"Model {latest.NonPydanticSchema} is not a subclass of BaseModel",
        ),
    ):
        create_local_simple_versioned_packages(
            schema(latest.NonPydanticSchema).field("foo").didnt_exist,
        )


def test__schema_defined_in_nested_files(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_with_empty_classes: _FakeModuleWithEmptyClasses,
    head_dir: Path,
    head_package_path: str,
    temp_data_dir: Path,
    temp_data_package_path: str,
):
    head_dir.joinpath("level0.py").write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        class Level0(BaseModel):
            foo: str
""",
        ),
    )
    level_1_dir = head_dir / "level1_dir"
    level_1_dir.mkdir()

    level_1_dir.joinpath("__init__.py").write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        class Level1Root(BaseModel):
            foo: str
        """,
        ),
    )
    level_1_dir.joinpath("level1.py").write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        class Level1(BaseModel):
            foo: str
""",
        ),
    )

    level_2_dir = level_1_dir / "level2_dir"
    level_2_dir.mkdir()
    level_2_dir.joinpath("__init__.py").write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        class Level2Root(BaseModel):
            foo: str
        """,
        ),
    )
    level_2_dir.joinpath("level2.py").write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        class Level2(BaseModel):
            foo: str
        """,
        ),
    )

    latest_level0 = importlib.import_module(head_package_path + ".level0")
    latest_level1_root = importlib.import_module(head_package_path + ".level1_dir")
    latest_level1 = importlib.import_module(head_package_path + ".level1_dir.level1")
    latest_level2_root = importlib.import_module(head_package_path + ".level1_dir.level2_dir")
    latest_level2 = importlib.import_module(head_package_path + ".level1_dir.level2_dir.level2")

    create_local_simple_versioned_packages(
        schema(latest_level0.Level0).field("foo").didnt_exist,
        schema(latest_level1_root.Level1Root).field("foo").didnt_exist,
        schema(latest_level1.Level1).field("foo").didnt_exist,
        schema(latest_level2_root.Level2Root).field("foo").didnt_exist,
        schema(latest_level2.Level2).field("foo").didnt_exist,
    )

    latest_alias_level0 = importlib.import_module(temp_data_package_path + ".v2001_01_01.level0")
    latest_alias_level1_root = importlib.import_module(temp_data_package_path + ".v2001_01_01.level1_dir")
    latest_alias_level1 = importlib.import_module(temp_data_package_path + ".v2001_01_01.level1_dir.level1")
    latest_alias_level2_root = importlib.import_module(temp_data_package_path + ".v2001_01_01.level1_dir.level2_dir")
    latest_alias_level2 = importlib.import_module(temp_data_package_path + ".v2001_01_01.level1_dir.level2_dir.level2")

    assert latest_alias_level0.Level0.__fields__.keys() == latest_level0.Level0.__fields__.keys()
    assert latest_alias_level1_root.Level1Root.__fields__.keys() == latest_level1_root.Level1Root.__fields__.keys()
    assert latest_alias_level1.Level1.__fields__.keys() == latest_level1.Level1.__fields__.keys()
    assert latest_alias_level2_root.Level2Root.__fields__.keys() == latest_level2_root.Level2Root.__fields__.keys()
    assert latest_alias_level2.Level2.__fields__.keys() == latest_level2.Level2.__fields__.keys()

    old_level0 = importlib.import_module(temp_data_package_path + ".v2000_01_01.level0")
    old_level1_root = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir")
    old_level1 = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir.level1")
    old_level2_root = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir.level2_dir")
    old_level2 = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir.level2_dir.level2")

    assert inspect.getsource(old_level0.Level0) == "class Level0(BaseModel):\n    pass\n"
    assert inspect.getsource(old_level1_root.Level1Root) == "class Level1Root(BaseModel):\n    pass\n"
    assert inspect.getsource(old_level1.Level1) == "class Level1(BaseModel):\n    pass\n"
    assert inspect.getsource(old_level2_root.Level2Root) == "class Level2Root(BaseModel):\n    pass\n"
    assert inspect.getsource(old_level2.Level2) == "class Level2(BaseModel):\n    pass\n"


@pytest.fixture()
def head_with_dependent_schemas(
    temp_data_package_path: str,
    create_local_versioned_packages: CreateLocalVersionedPackages,
    head_module_for: HeadModuleFor,
    head_dir: Path,
) -> Any:
    imports = "from pydantic import Field, BaseModel\n"
    schema_with_one_float_field = """
class SchemaWithOneFloatField(BaseModel):
    foo: float
    """
    schema_that_depends_on_another_schema = """
class SchemaThatDependsOnAnotherSchema(SchemaWithOneFloatField):
    foo: SchemaWithOneFloatField
    bat: SchemaWithOneFloatField | int = Field(default=SchemaWithOneFloatField(foo=3.14))
    dat: "SchemaWithOneFloatField | int"

    def baz(self, daz: SchemaWithOneFloatField) -> SchemaWithOneFloatField:
        return SchemaWithOneFloatField(foo=3.14)
"""
    latest = head_module_for(imports + schema_with_one_float_field + schema_that_depends_on_another_schema)
    head_dir.joinpath("another_file.py").write_text(
        "from . import SchemaWithOneFloatField\n" + imports + schema_that_depends_on_another_schema,
    )
    return latest


def test__schema_had_name__with_dependent_schema_not_altered(
    temp_data_package_path: str,
    create_local_versioned_packages: CreateLocalVersionedPackages,
    head_with_dependent_schemas,
):
    v1, v2, _ = create_local_versioned_packages(
        version_change(schema(head_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema2")),
        version_change(schema(head_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema")),
    )
    another_file_v1 = importlib.import_module(temp_data_package_path + ".v2000_01_01.another_file")
    another_file_v2 = importlib.import_module(temp_data_package_path + ".v2001_01_01.another_file")

    assert inspect.getsource(v1.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n"
        "    dat: 'MyFloatySchema2 | int'\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert inspect.getsource(v2.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n"
        "    dat: 'MyFloatySchema | int'\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )

    assert inspect.getsource(another_file_v1.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n"
        "    dat: 'MyFloatySchema2 | int'\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert inspect.getsource(another_file_v2.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n"
        "    dat: 'MyFloatySchema | int'\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )


def test__schema_had_name__with_dependent_schema_altered(
    temp_data_package_path: str,
    create_local_versioned_packages: CreateLocalVersionedPackages,
    head_module_for: HeadModuleFor,
    head_dir: Path,
    head_with_dependent_schemas,
    head_package_path,
):
    another_file = importlib.import_module(head_package_path + ".another_file")

    v1, v2, _ = create_local_versioned_packages(
        version_change(
            schema(head_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema2"),
        ),
        version_change(
            schema(head_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema"),
            schema(head_with_dependent_schemas.SchemaThatDependsOnAnotherSchema).field("foo").had(alias="hewwo"),
            schema(another_file.SchemaThatDependsOnAnotherSchema).field("foo").had(alias="bar"),
        ),
    )

    another_file_v1 = importlib.import_module(temp_data_package_path + ".v2000_01_01.another_file")
    another_file_v2 = importlib.import_module(temp_data_package_path + ".v2001_01_01.another_file")

    assert (
        inspect.getsource(v1.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2 = Field(alias='hewwo')\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n"
        "    dat: 'MyFloatySchema2 | int'\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert (
        inspect.getsource(v2.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema = Field(alias='hewwo')\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n"
        "    dat: 'MyFloatySchema | int'\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )

    assert (
        inspect.getsource(another_file_v1.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2 = Field(alias='bar')\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n"
        "    dat: 'MyFloatySchema2 | int'\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert (
        inspect.getsource(another_file_v2.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema = Field(alias='bar')\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n"
        "    dat: 'MyFloatySchema | int'\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )


def test__schema_had_name__trying_to_assign_to_the_same_name__should_raise_error(
    create_versioned_packages: CreateVersionedPackages,
    head_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of "EmptySchema" in "MyVersionChange" '
            "but it already has the name you tried to assign.",
        ),
    ):
        create_versioned_packages(
            version_change(
                schema(head_with_empty_classes.EmptySchema).had(name="EmptySchema"),
            ),
        )


def test__codegen_variable_and_random_expression_migration(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_module_for: HeadModuleFor,
) -> None:
    head_module_for("a, b = 1, 2")
    v1 = create_local_simple_versioned_packages()
    assert v1.a == 1
    assert v1.b == 2


def test__codegen_correct_docstring_handling(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_module_for: HeadModuleFor,
) -> None:
    latest = head_module_for(
        """
from pydantic import BaseModel
class MyClass(BaseModel):
    ''' Hewwo
    Darkness my old friend I've come to talk with you again''' """,
    )
    v1 = create_local_simple_versioned_packages(schema(latest.MyClass).field("foo").existed_as(type=str))
    assert inspect.getsource(v1.MyClass) == (
        "class MyClass(BaseModel):\n"
        '    """ Hewwo\n'
        '    Darkness my old friend I\'ve come to talk with you again"""\n'
        "    foo: str\n"
    )


def test__codegen_correct_indent_handling(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_module_for: HeadModuleFor,
) -> None:
    latest = head_module_for(
        """
from pydantic import BaseModel
if True:
    class ConfigMixin(BaseModel):
        pass

class MyClass(ConfigMixin):
    foo: str

    """,
    )
    v1 = create_local_simple_versioned_packages(schema(latest.MyClass).field("bar").existed_as(type=str))
    assert inspect.getsource(v1.MyClass) == ("class MyClass(ConfigMixin):\n" "    foo: str\n    bar: str\n")


def test__codegen_preserves_arbitrary_expressions(
    create_local_simple_versioned_packages: CreateLocalSimpleVersionedPackages,
    head_module_for: HeadModuleFor,
) -> None:
    head_module_for("'abc'")
    v1 = create_local_simple_versioned_packages()
    assert inspect.getsource(v1) == (
        '''# THIS FILE WAS AUTO-GENERATED BY CADWYN. DO NOT EVER TRY TO EDIT IT BY HAND\n\n"""abc"""\n'''
    )
