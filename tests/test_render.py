import re
import sys
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, ClassVar

import pytest
from pydantic import AfterValidator, BaseModel, Field

from cadwyn import VersionChange, schema
from cadwyn._render import render_model, render_model_by_path, render_module_by_path
from cadwyn.exceptions import ImportFromStringError
from cadwyn.schema_generation import generate_versioned_models
from cadwyn.structure.versions import Version, VersionBundle
from tests.conftest import version_change
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


class RenderedDocumentedModel(BaseModel):
    """INTERNAL: head-only implementation note."""

    foo: str


class RenderedUndocumentedModel(BaseModel):
    foo: str


class DescribeRenderedModels(VersionChange):
    description = "Describe the rendered models"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedDocumentedModel).had(description="A public description."),
        schema(RenderedUndocumentedModel).had(description="Another public description."),
    )


class RenderedModelWithMultilineDescription(BaseModel):
    foo: str


class RenderedModelWithDocumentedClassVar(BaseModel):
    """Head description."""

    internal: ClassVar[str] = "private"
    """Internal implementation notes."""


class RemoveDescriptionOfRenderedModel(VersionChange):
    description = "Remove the description of the rendered model"
    instructions_to_migrate_to_previous_version = (
        schema(RenderedDocumentedModel).had(description=None),
        schema(RenderedModelWithDocumentedClassVar).had(description=None),
        schema(RenderedModelWithMultilineDescription).had(description="  First line.\n    Second line.\n"),
    )


# Version changes can only belong to one version bundle
VERSIONS_WITH_REMOVED_DESCRIPTIONS = VersionBundle(
    Version("2001-01-01", RemoveDescriptionOfRenderedModel), Version("2000-01-01")
)


def assert_rendered_model_has_the_description_of_generated_model(
    model: type[BaseModel], versions: VersionBundle, version: str
) -> None:
    rendered_namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field, "ClassVar": ClassVar}
    exec(render_model(model, versions, version), rendered_namespace)
    generated_model = generate_versioned_models(versions)[version][model]
    rendered_schema = rendered_namespace[model.__name__].model_json_schema()
    assert rendered_schema.get("description") == generated_model.model_json_schema().get("description")


def test__render_model__schema_had_description__should_render_description_of_the_version():
    versions = VersionBundle(Version("2001-01-01", DescribeRenderedModels), Version("2000-01-01"))

    assert code(render_model(RenderedDocumentedModel, versions, "2000-01-01")) == code(
        '''
class RenderedDocumentedModel(BaseModel):
    """A public description."""
    foo: str = Field()
'''
    )
    assert code(render_model(RenderedUndocumentedModel, versions, "2000-01-01")) == code(
        '''
class RenderedUndocumentedModel(BaseModel):
    """Another public description."""
    foo: str = Field()
'''
    )
    assert code(render_model(RenderedDocumentedModel, versions, "2001-01-01")) == code(
        '''
class RenderedDocumentedModel(BaseModel):
    """INTERNAL: head-only implementation note."""
    foo: str = Field()
'''
    )


def test__render_model__schema_had_description_none__should_render_model_without_docstring():
    versions = VERSIONS_WITH_REMOVED_DESCRIPTIONS

    assert code(render_model(RenderedDocumentedModel, versions, "2000-01-01")) == code(
        """
class RenderedDocumentedModel(BaseModel):
    foo: str = Field()
"""
    )
    assert_rendered_model_has_the_description_of_generated_model(RenderedDocumentedModel, versions, "2000-01-01")


def test__render_model__schema_had_description_none__with_documented_class_var__should_not_render_docstring():
    versions = VERSIONS_WITH_REMOVED_DESCRIPTIONS

    # The documentation of the class var would become the docstring of the class if it was first
    assert code(render_model(RenderedModelWithDocumentedClassVar, versions, "2000-01-01")) == code(
        """
class RenderedModelWithDocumentedClassVar(BaseModel):
    pass
"""
    )
    assert_rendered_model_has_the_description_of_generated_model(
        RenderedModelWithDocumentedClassVar, versions, "2000-01-01"
    )


def test__render_model__schema_had_multiline_description__should_render_the_same_description():
    versions = VERSIONS_WITH_REMOVED_DESCRIPTIONS

    assert_rendered_model_has_the_description_of_generated_model(
        RenderedModelWithMultilineDescription, versions, "2000-01-01"
    )


class RenderedModelWithDescriptionInAnnotated(BaseModel):
    foo: Annotated[str, Field(description="Foo")] = "x"


def test__render_model__didnt_have_description_in_annotated__should_render_annotation_without_it():
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(schema(RenderedModelWithDescriptionInAnnotated).field("foo").didnt_have("description")),
        ),
        Version("2000-01-01"),
    )

    # Like the generated model. Before, `cadwyn render` kept the description in the annotation
    assert (
        generate_versioned_models(versions)["2000-01-01"][RenderedModelWithDescriptionInAnnotated]
        .model_fields["foo"]
        .description
        is None
    )
    assert "Foo" in render_model(RenderedModelWithDescriptionInAnnotated, versions, "2001-01-01")
    assert "Foo" not in render_model(RenderedModelWithDescriptionInAnnotated, versions, "2000-01-01")
