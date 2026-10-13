import ast
import re
import sys
from datetime import datetime
from enum import Enum
from typing import Annotated, Any

import pytest
from pydantic import AfterValidator, BaseModel, Field

from cadwyn import VersionChange, schema
from cadwyn._render import render_model, render_model_by_path, render_module_by_path
from cadwyn.exceptions import ImportFromStringError
from cadwyn.schema_generation import generate_versioned_models
from cadwyn.structure.versions import Version, VersionBundle
from tests.test_cli import code


def test__render_model__with_weird_types():
    result = render_model_by_path(
        "tests._resources.render.complex.classes:ModelWithWeirdFields",
        "tests._resources.render.complex.versions:app",
        "2000-01-01",
    )
    if sys.version_info >= (3, 11):
        rendered_lambda = "lambda: 91"
    else:
        rendered_lambda = "lambda : 91"

    if sys.version_info >= (3, 13):
        rend_ann = "typing.Annotated"
    else:
        rend_ann = "Annotated"

    rend_len = "Len(min_length=0, max_length=None)"
    rend_interval = "Interval(gt=12, ge=None, lt=None, le=None)"

    # TODO: As you see, we do not rename bases correctly in render. We gotta fix it some day...
    assert code(result) == code(
        f'''
class ModelWithWeirdFields(A):
    """My docstring"""
    foo: dict = Field(default={{'a': 'b'}})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: typing.Literal[MyEnum.foo] = Field()
    saz: {rend_ann}[str, StringConstraints(to_upper=True)] = Field()
    laz: {rend_ann}[int, None, {rend_interval}, None] = Field(gt=12)
    taz: typing.Union[int, str, None] = Field(default_factory={rendered_lambda})
    naz: list[int] = Field(default=[1, 2, 3])
    gaz: {rend_ann}[bytes, Strict(strict=True), {rend_len}] = Field(title='Hewwo', min_length=3)
'''
    )


def test__render_model__with_non_empty_enum():
    result = render_model_by_path(
        "tests._resources.render.complex.classes:MyEnum",
        "tests._resources.render.complex.versions:app",
        "2000-01-01",
    )
    assert code(result) == code(
        """
class MyEnum(Enum):
    foo = 1
"""
    )


def test__render_model__with_unversioned_enum():
    versions = VersionBundle(Version("2000-01-01"))

    class RandomEnum(Enum):
        foo = 1

    result = render_model(RandomEnum, versions, "2000-01-01")
    assert code(result) == code(
        """
class RandomEnum(Enum):
    foo = 1
"""
    )


def test__render_model__with_non_class__should_raise_error():
    with pytest.raises(
        TypeError, match=re.escape("tests._resources.render.complex.classes.my_default_factory is not a class")
    ):
        render_model_by_path(
            "tests._resources.render.complex.classes:my_default_factory",
            "tests._resources.render.complex.versions:app",
            "2000-01-01",
        )


def test__render_model__with_non_existent_module():
    with pytest.raises(
        ImportFromStringError, match=re.escape('Could not import module "tests._resources.render.complex.KWASSES".')
    ):
        render_model_by_path("tests._resources.render.complex.KWASSES:ModelWithWeirdFields", "", "")


def test__render_module__with_non_existent_module():
    with pytest.raises(
        ImportFromStringError, match=re.escape('Could not import module "tests._resources.render.complex.KWASSES".')
    ):
        render_module_by_path("tests._resources.render.complex.KWASSES", "", "")


def test__render_model__with_non_existent_attribute():
    with pytest.raises(
        ImportFromStringError,
        match=re.escape('Attribute "MODEW" not found in module "tests._resources.render.complex.classes".'),
    ):
        render_model_by_path("tests._resources.render.complex.classes:MODEW", "", "")


def test__render_model__with_wrong_format():
    with pytest.raises(
        ImportFromStringError,
        match=re.escape(
            'Import string "tests._resources.render.complex.classes" must be in format "<module>:<attribute>".'
        ),
    ):
        render_model_by_path("tests._resources.render.complex.classes", "", "")


class RenderedParent(BaseModel):
    foo: str = Field(max_length=10)
    bar: int = 0


class RenderedLeftChild(RenderedParent):
    pass


class RenderedRightChild(RenderedParent):
    pass


class RenderedDiamond(RenderedLeftChild, RenderedRightChild):
    pass


class TightenRightChild(VersionChange):
    description = "Tighten the right child"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedParent).field("bar").had(description="Bar"),
        schema(RenderedRightChild).field("foo").had(max_length=3),
    )


def test__render_model__diamond_whose_other_parent_changed_inherited_field__should_inherit_it_from_first_base():
    versions = VersionBundle(Version("2001-01-01", TightenRightChild), Version("2000-01-01"))

    assert code(render_model(RenderedDiamond, versions, "2000-01-01")) == code(
        """
class RenderedDiamond(RenderedLeftChild, RenderedRightChild):
    pass
"""
    )
    # The rendered models behave like the ones that Cadwyn generates
    rendered_namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field}
    generated_models = generate_versioned_models(versions)["2000-01-01"]
    for model in (RenderedParent, RenderedLeftChild, RenderedRightChild, RenderedDiamond):
        exec(render_model(model, versions, "2000-01-01"), rendered_namespace)
        rendered_schema = rendered_namespace[model.__name__].model_json_schema()
        assert rendered_schema["properties"]["foo"] == generated_models[model].model_json_schema()["properties"]["foo"]


class ChangeTypeOfLeftChildAndDescriptionOfRightChild(VersionChange):
    description = "Change the type of the left child and the description of the right child"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedParent).field("bar").had(description="Bar"),
        schema(RenderedLeftChild).field("foo").had(type=bytes),
        schema(RenderedRightChild).field("foo").had(description="Right"),
    )


def test__render_model__sibling_changed_type_of_inherited_field__should_render_the_type_of_the_model():
    versions = VersionBundle(
        Version("2001-01-01", ChangeTypeOfLeftChildAndDescriptionOfRightChild), Version("2000-01-01")
    )

    assert code(render_model(RenderedRightChild, versions, "2000-01-01")) == code(
        """
class RenderedRightChild(RenderedParent):
    foo: str = Field(max_length=10, description='Right')
"""
    )


def normalize(value: str) -> str:
    return value.strip()


class RenderedValidatedParent(BaseModel):
    foo: Annotated[str, AfterValidator(normalize)]
    bar: int = 0


class RenderedValidatedLeftChild(RenderedValidatedParent):
    pass


class RenderedValidatedRightChild(RenderedValidatedParent):
    pass


class RenderedValidatedDiamond(RenderedValidatedLeftChild, RenderedValidatedRightChild):
    pass


class DescribeValidatedRightChild(VersionChange):
    description = "Describe the field of the right child"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedValidatedParent).field("bar").had(description="Bar"),
        schema(RenderedValidatedRightChild).field("foo").had(description="Right"),
    )


def test__render_model__diamond_with_functional_validator_in_inherited_field__should_render_valid_python():
    versions = VersionBundle(Version("2001-01-01", DescribeValidatedRightChild), Version("2000-01-01"))

    rendered = render_model(RenderedValidatedDiamond, versions, "2000-01-01")

    compile(rendered, "<rendered>", "exec")
    generated_diamond = generate_versioned_models(versions)["2000-01-01"][RenderedValidatedDiamond]
    assert generated_diamond.model_validate({"foo": " value "}).foo == "value"
    assert generated_diamond.model_fields["foo"].description is None


class RenderedParentWithFactory(BaseModel):
    created_at: datetime = Field(default_factory=datetime.now)
    bar: int = 0


class RenderedLeftChildWithFactory(RenderedParentWithFactory):
    pass


class RenderedRightChildWithFactory(RenderedParentWithFactory):
    pass


class RenderedDiamondWithFactory(RenderedLeftChildWithFactory, RenderedRightChildWithFactory):
    pass


class DescribeRightChildWithFactory(VersionChange):
    description = "Describe the field of the right child"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedParentWithFactory).field("bar").had(description="Bar"),
        schema(RenderedRightChildWithFactory).field("created_at").had(description="Right"),
    )


def test__render_model__diamond_whose_other_parent_changed_field_with_default_factory__should_render_valid_python():
    versions = VersionBundle(Version("2001-01-01", DescribeRightChildWithFactory), Version("2000-01-01"))

    rendered = render_model(RenderedDiamondWithFactory, versions, "2000-01-01")

    compile(rendered, "<rendered>", "exec")


class RenderedAliases(BaseModel):
    deleted_alias: str = Field(alias="a", validation_alias="v", serialization_alias="s")
    deleted_derived_aliases: str = Field(alias="b")
    changed_alias: str = Field(alias="c")


class RenderedAnnotatedAlias(BaseModel):
    deleted_alias: Annotated[str, Field(alias="a")]


class ChangeRenderedAliases(VersionChange):
    description = "Change the aliases"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedAliases).field("deleted_alias").didnt_have("alias"),
        schema(RenderedAliases).field("deleted_derived_aliases").didnt_have("alias"),
        schema(RenderedAliases).field("changed_alias").had(alias="old"),
        schema(RenderedAnnotatedAlias).field("deleted_alias").didnt_have("alias"),
    )


def test__render_model__deleted_alias__should_only_render_none_aliases_that_override_the_annotation():
    versions = VersionBundle(Version("2001-01-01", ChangeRenderedAliases), Version("2000-01-01"))

    rendered = render_model(RenderedAliases, versions, "2000-01-01")
    assert code(rendered) == code(
        """
class RenderedAliases(BaseModel):
    deleted_alias: str = Field(validation_alias='v', serialization_alias='s')
    deleted_derived_aliases: str = Field()
    changed_alias: str = Field(alias='old', validation_alias='old', serialization_alias='old')
"""
    )
    # The rendered model behaves like the one that Cadwyn generates
    rendered_namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field}
    exec(rendered, rendered_namespace)
    generated_model = generate_versioned_models(versions)["2000-01-01"][RenderedAliases]
    for mode in ("validation", "serialization"):
        assert rendered_namespace["RenderedAliases"].model_json_schema(mode=mode) == generated_model.model_json_schema(
            mode=mode
        )

    # The Field() in the annotation still has the aliases so only the None ones override them
    rendered_annotated = render_model(RenderedAnnotatedAlias, versions, "2000-01-01")
    class_definition = ast.parse(rendered_annotated).body[0]
    assert isinstance(class_definition, ast.ClassDef)
    field_definition = class_definition.body[0]
    assert isinstance(field_definition, ast.AnnAssign)
    assert isinstance(field_definition.value, ast.Call)
    assert {keyword.arg: ast.literal_eval(keyword.value) for keyword in field_definition.value.keywords} == {
        "alias": None,
        "validation_alias": None,
        "serialization_alias": None,
    }
