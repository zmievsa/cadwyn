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

from cadwyn import generate_code_for_versioned_packages
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
    CreateLocalSimpleVersionedSchemas,
    CreateLocalVersionedSchemas,
    CreateVersionedSchemas,
    LatestModuleFor,
    _FakeModuleWithEmptyClasses,
    version_change,
)


def test__codegen_dont_add_pragma_no_cover_to_imports_to_latest(
    create_local_versioned_schemas: CreateLocalVersionedSchemas,
    latest_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    generated_version = create_local_versioned_schemas(ignore_coverage_for_latest_aliases=False)[0]
    assert inspect.getsource(generated_version) == (
        "# THIS FILE WAS AUTO-GENERATED BY CADWYN. DO NOT EVER TRY TO EDIT IT BY HAND\n\n"
        "from ..latest import * # noqa: F403\n"
    )


def test__codegen_add_pragma_no_cover_to_imports_to_latest(
    create_local_versioned_schemas: CreateLocalVersionedSchemas,
    latest_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    generated_version = create_local_versioned_schemas(ignore_coverage_for_latest_aliases=True)[0]
    assert inspect.getsource(generated_version) == (
        "# THIS FILE WAS AUTO-GENERATED BY CADWYN. DO NOT EVER TRY TO EDIT IT BY HAND\n\n"
        "from ..latest import * # noqa: F403 # pragma: no cover\n"
    )


def test__with_file_instead_of_package__error(
    latest_package_path: str,
    latest_dir: Path,
    api_version_var: ContextVar[date | None],
):
    (latest_dir / "hello.py").touch()
    wrong_latest_module = importlib.import_module(latest_package_path + ".hello")

    with pytest.raises(
        CodeGenerationError,
        match=re.escape(f'Module "{wrong_latest_module}" is not a package'),
    ):
        generate_code_for_versioned_packages(
            wrong_latest_module,
            VersionBundle(
                Version(date(2000, 1, 1)),
                api_version_var=api_version_var,
            ),
        )


def test__non_python_files__copied_to_all_dirs(
    create_local_simple_versioned_schemas: CreateLocalSimpleVersionedSchemas,
    latest_with_empty_classes: _FakeModuleWithEmptyClasses,
    latest_dir: Path,
    temp_data_dir: Path,
):
    json_file_dir = latest_dir / "json_files"
    json_file_dir.mkdir()
    json_file_dir.joinpath("foo.json").write_text('{"hello":"world"}')

    create_local_simple_versioned_schemas()
    assert json.loads(Path(temp_data_dir / "v2000_01_01/json_files/foo.json").read_text()) == {"hello": "world"}
    assert json.loads(Path(temp_data_dir / "v2001_01_01/json_files/foo.json").read_text()) == {"hello": "world"}


def test__non_pydantic_schema__error(
    create_local_simple_versioned_schemas: CreateLocalSimpleVersionedSchemas,
    latest_module_for: LatestModuleFor,
):
    latest = latest_module_for(
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
        create_local_simple_versioned_schemas(
            schema(latest.NonPydanticSchema).field("foo").didnt_exist,
        )


def test__schema_defined_in_non_init_nested_files(
    create_local_simple_versioned_schemas: CreateLocalSimpleVersionedSchemas,
    latest_with_empty_classes: _FakeModuleWithEmptyClasses,
    latest_dir: Path,
    latest_package_path: str,
    temp_data_dir: Path,
    temp_data_package_path: str,
):
    latest_dir.joinpath("level0.py").write_text(
        textwrap.dedent(
            """
        from pydantic import BaseModel
        class Level0(BaseModel):
            foo: str
""",
        ),
    )
    level_1_dir = latest_dir / "level1_dir"
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

    latest_level0 = importlib.import_module(latest_package_path + ".level0")
    latest_level1_root = importlib.import_module(latest_package_path + ".level1_dir")
    latest_level1 = importlib.import_module(latest_package_path + ".level1_dir.level1")
    latest_level2_root = importlib.import_module(latest_package_path + ".level1_dir.level2_dir")
    latest_level2 = importlib.import_module(latest_package_path + ".level1_dir.level2_dir.level2")

    create_local_simple_versioned_schemas(
        schema(latest_level0.Level0).field("foo").didnt_exist,
        schema(latest_level1_root.Level1Root).field("foo").didnt_exist,
        schema(latest_level1.Level1).field("foo").didnt_exist,
        schema(latest_level2_root.Level2Root).field("foo").didnt_exist,
        schema(latest_level2.Level2).field("foo").didnt_exist,
    )

    latest_level0 = importlib.import_module(temp_data_package_path + ".v2000_01_01.level0")
    latest_level1_root = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir")
    latest_level1 = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir.level1")
    latest_level2_root = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir.level2_dir")
    latest_level2 = importlib.import_module(temp_data_package_path + ".v2000_01_01.level1_dir.level2_dir.level2")

    assert inspect.getsource(latest_level0.Level0) == "class Level0(BaseModel):\n    pass\n"
    assert inspect.getsource(latest_level1_root.Level1Root) == "class Level1Root(BaseModel):\n    pass\n"
    assert inspect.getsource(latest_level1.Level1) == "class Level1(BaseModel):\n    pass\n"
    assert inspect.getsource(latest_level2_root.Level2Root) == "class Level2Root(BaseModel):\n    pass\n"
    assert inspect.getsource(latest_level2.Level2) == "class Level2(BaseModel):\n    pass\n"


@pytest.fixture()
def latest_with_dependent_schemas(
    temp_data_package_path: str,
    create_local_versioned_schemas: CreateLocalVersionedSchemas,
    latest_module_for: LatestModuleFor,
    latest_dir: Path,
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

    def baz(self, daz: SchemaWithOneFloatField) -> SchemaWithOneFloatField:
        return SchemaWithOneFloatField(foo=3.14)
"""
    latest = latest_module_for(imports + schema_with_one_float_field + schema_that_depends_on_another_schema)
    latest_dir.joinpath("another_file.py").write_text(
        "from . import SchemaWithOneFloatField\n" + imports + schema_that_depends_on_another_schema,
    )
    return latest


def test__schema_had_name__with_dependent_schema_not_altered(
    temp_data_package_path: str,
    create_local_versioned_schemas: CreateLocalVersionedSchemas,
    latest_with_dependent_schemas,
):
    v1, v2, _ = create_local_versioned_schemas(
        version_change(schema(latest_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema2")),
        version_change(schema(latest_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema")),
    )
    another_file_v1 = importlib.import_module(temp_data_package_path + ".v2000_01_01.another_file")
    another_file_v2 = importlib.import_module(temp_data_package_path + ".v2001_01_01.another_file")

    assert inspect.getsource(v1.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert inspect.getsource(v2.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )

    assert inspect.getsource(another_file_v1.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert inspect.getsource(another_file_v2.SchemaThatDependsOnAnotherSchema) == (
        "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )


def test__schema_had_name__with_dependent_schema_altered(
    temp_data_package_path: str,
    create_local_versioned_schemas: CreateLocalVersionedSchemas,
    latest_module_for: LatestModuleFor,
    latest_dir: Path,
    latest_with_dependent_schemas,
    latest_package_path,
):
    another_file = importlib.import_module(latest_package_path + ".another_file")

    v1, v2, _ = create_local_versioned_schemas(
        version_change(
            schema(latest_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema2"),
        ),
        version_change(
            schema(latest_with_dependent_schemas.SchemaWithOneFloatField).had(name="MyFloatySchema"),
            schema(latest_with_dependent_schemas.SchemaThatDependsOnAnotherSchema).field("foo").had(alias="hewwo"),
            schema(another_file.SchemaThatDependsOnAnotherSchema).field("foo").had(alias="bar"),
        ),
    )

    another_file_v1 = importlib.import_module(temp_data_package_path + ".v2000_01_01.another_file")
    another_file_v2 = importlib.import_module(temp_data_package_path + ".v2001_01_01.another_file")

    assert (
        inspect.getsource(v1.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2 = Field(alias='hewwo')\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert (
        inspect.getsource(v2.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema = Field(alias='hewwo')\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )

    assert (
        inspect.getsource(another_file_v1.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema2):\n"
        "    foo: MyFloatySchema2 = Field(alias='bar')\n"
        "    bat: MyFloatySchema2 | int = Field(default=MyFloatySchema2(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema2) -> MyFloatySchema2:\n"
        "        return MyFloatySchema2(foo=3.14)\n"
    )
    assert (
        inspect.getsource(another_file_v2.SchemaThatDependsOnAnotherSchema)
        == "class SchemaThatDependsOnAnotherSchema(MyFloatySchema):\n"
        "    foo: MyFloatySchema = Field(alias='bar')\n"
        "    bat: MyFloatySchema | int = Field(default=MyFloatySchema(foo=3.14))\n\n"
        "    def baz(self, daz: MyFloatySchema) -> MyFloatySchema:\n"
        "        return MyFloatySchema(foo=3.14)\n"
    )


def test__schema_had_name__trying_to_assign_to_the_same_name__should_raise_error(
    create_versioned_schemas: CreateVersionedSchemas,
    latest_with_empty_classes: _FakeModuleWithEmptyClasses,
):
    with pytest.raises(
        InvalidGenerationInstructionError,
        match=re.escape(
            'You tried to change the name of "EmptySchema" in "MyVersionChange" '
            "but it already has the name you tried to assign.",
        ),
    ):
        create_versioned_schemas(
            version_change(
                schema(latest_with_empty_classes.EmptySchema).had(name="EmptySchema"),
            ),
        )


def test__codegen_variable_and_random_expression_migration(
    create_local_simple_versioned_schemas: CreateLocalSimpleVersionedSchemas,
    latest_module_for: LatestModuleFor,
) -> None:
    latest_module_for("a, b = 1, 2")
    v1 = create_local_simple_versioned_schemas()
    assert inspect.getsource(v1).endswith("(a, b) = (1, 2)\n")


def test__codegen_correct_docstring_handling(
    create_local_simple_versioned_schemas: CreateLocalSimpleVersionedSchemas,
    latest_module_for: LatestModuleFor,
) -> None:
    latest = latest_module_for(
        """
from pydantic import BaseModel
class MyClass(BaseModel):
    ''' Hewwo
    Darkness my old friend I've come to talk with you again''' """,
    )
    v1 = create_local_simple_versioned_schemas(schema(latest.MyClass).field("foo").existed_as(type=str))
    assert inspect.getsource(v1.MyClass) == (
        "class MyClass(BaseModel):\n"
        '    """ Hewwo\n'
        '    Darkness my old friend I\'ve come to talk with you again"""\n'
        "    foo: str\n"
    )
