import re
import sys
from collections import Counter, OrderedDict, defaultdict, namedtuple
from collections.abc import Iterator
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Annotated, Any, ClassVar, NamedTuple

import pytest
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from typing_extensions import override

from cadwyn import VersionChange, schema
from cadwyn._render import render_model, render_model_by_path, render_module_by_path
from cadwyn.exceptions import CadwynRenderError, ImportFromStringError
from cadwyn.schema_generation import generate_versioned_models
from cadwyn.structure.versions import Version, VersionBundle
from tests.conftest import version_change
from tests.test_cli import code


class Point(NamedTuple):
    x: int
    y: int


class RecursivePoint(NamedTuple):
    values: list[Any]


EmptyPoint = namedtuple("EmptyPoint", ())  # noqa: PYI024 # Cover collections.namedtuple as well as typing.NamedTuple


class ModelWithContainerDefault(BaseModel):
    value: Any = None


def missing_count() -> int:
    return 5


def render_container_default(default: Any) -> str:
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(ModelWithContainerDefault).field("value").had(default=default))),
        Version("2000-01-01"),
    )
    return render_model(ModelWithContainerDefault, versions, "2000-01-01")


def evaluate_rendered_default(default: Any) -> Any:
    namespace: dict[str, Any] = dict(globals())
    exec(render_container_default(default), namespace)
    return namespace["ModelWithContainerDefault"].model_fields["value"].default


@pytest.mark.parametrize(
    "default",
    [
        Point(1, 2),
        EmptyPoint(),
        RecursivePoint([None, int, list[int]]),
        ("one",),
        (None, int),
        (),
        [],
        [None, int, list[int]],
        type(None),
        [None, type(None)],
        {},
        {None: None, int: list[int]},
        set(),
        frozenset(),
        {None, int},
        frozenset({None, int}),
    ],
)
def test__render_model__container_default__should_recreate_its_type_and_value(default: Any):
    rendered_default = evaluate_rendered_default(default)
    assert type(rendered_default) is type(default)
    assert rendered_default == default


@pytest.mark.parametrize(("factory", "missing_value"), [(int, 0), (missing_count, 5), (list, []), (type(None), None)])
def test__render_model__defaultdict_default__should_keep_items_and_factory(factory: Any, missing_value: Any):
    rendered_default = evaluate_rendered_default(defaultdict(factory, a=1, empty=None))
    assert type(rendered_default) is defaultdict
    assert dict(rendered_default) == {"a": 1, "empty": None}
    assert rendered_default.default_factory is factory
    assert rendered_default["missing"] == missing_value


def test__render_model__defaultdict_without_factory__should_keep_missing_key_error():
    rendered_default = evaluate_rendered_default(defaultdict(None, a=1))
    assert type(rendered_default) is defaultdict
    assert dict(rendered_default) == {"a": 1}
    assert rendered_default.default_factory is None
    with pytest.raises(KeyError):
        rendered_default["missing"]


def test__render_model__ordered_dict_default__should_keep_insertion_order():
    default = OrderedDict((("z", None), ("a", int)))
    rendered_default = evaluate_rendered_default(default)
    assert type(rendered_default) is OrderedDict
    assert list(rendered_default.items()) == list(default.items())


@pytest.mark.parametrize("default", [Counter(a=3, b=0, c=-2), Counter()])
def test__render_model__counter_default__should_keep_exact_counts(default: Counter[str]):
    rendered_default = evaluate_rendered_default(default)
    assert type(rendered_default) is Counter
    assert dict(rendered_default) == dict(default)
    assert rendered_default["missing"] == 0


def test__render_model__nested_container_defaults__should_keep_types_keys_and_factory():
    default = [Point(1, 2), {Point(3, 4): defaultdict(int, a=1), "counts": Counter(a=3)}]
    rendered_default = evaluate_rendered_default(default)
    assert rendered_default == default
    assert type(rendered_default[0]) is Point
    key = next(iter(rendered_default[1]))
    assert type(key) is Point
    assert type(rendered_default[1][key]) is defaultdict
    assert rendered_default[1][key].default_factory is int
    assert type(rendered_default[1]["counts"]) is Counter


class CustomList(list):
    pass


class CustomTuple(tuple):
    __slots__ = ()


class CustomDict(dict):
    pass


class CustomSet(set):
    pass


class CustomFrozenSet(frozenset):
    pass


class ConstructorList(list):
    def __init__(self, values: list[int], *, label: str) -> None:
        super().__init__(values)
        self.label = label


@pytest.mark.parametrize(
    "default", [CustomList([1]), CustomTuple((1,)), CustomDict(a=1), ConstructorList([1], label="keep")]
)
def test__render_model__custom_container_default__should_fail_instead_of_losing_its_type(default: Any):
    with pytest.raises(
        CadwynRenderError, match=rf"ModelWithContainerDefault.value.*{type(default).__name__}.*default_factory"
    ):
        render_container_default(default)


class RepresentationList(list):
    @override
    def __repr__(self) -> str:
        return f"RepresentationList({list.__repr__(self)})"


class ReferencedList(list):
    @override
    def __repr__(self) -> str:
        return "REFERENCED_LIST"


class CopyingList(list):
    @override
    def __iter__(self) -> Iterator[list[int]]:
        for value in list.__iter__(self):
            yield list(value)

    @override
    def __repr__(self) -> str:
        return f"CopyingList({list.__repr__(self)})"


REFERENCED_LIST = ReferencedList([1, 2])


def test__render_model__subclass_iterator_yields_fresh_containers__should_preserve_main_source_bytes():
    default = CopyingList([[i] for i in range(100)])
    assert render_container_default(default) == (
        f"class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default={default!r})"
    )


def test__render_model__shared_subclass_with_reference_repr__should_preserve_main_source_bytes():
    assert render_container_default([REFERENCED_LIST, REFERENCED_LIST]) == (
        "class ModelWithContainerDefault(BaseModel):\n"
        "    value: Any = Field(default=[REFERENCED_LIST, REFERENCED_LIST])"
    )
    restored = evaluate_rendered_default([REFERENCED_LIST, REFERENCED_LIST])
    assert restored[0] is restored[1] is REFERENCED_LIST


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        (CustomSet({1}), "CustomSet({1})"),
        (CustomFrozenSet({1}), "CustomFrozenSet({1})"),
        (RepresentationList([1, 2]), "RepresentationList([1, 2])"),
        (CopyingList([[1], [2], [3]]), "CopyingList([[1], [2], [3]])"),
    ],
)
def test__render_model__reconstructible_subclass__should_preserve_main_source_bytes(default: Any, expected: str):
    assert render_container_default(default) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=" + expected + ")"
    )
    rendered_default = evaluate_rendered_default(default)
    assert type(rendered_default) is type(default)
    assert rendered_default == default


@pytest.mark.parametrize(
    ("shared", "expected"),
    [
        ((), "[(), ()]"),
        ((1, 2), "[(1, 2), (1, 2)]"),
        (frozenset({1}), "[frozenset({1}), frozenset({1})]"),
    ],
)
def test__render_model__repeated_immutable_container__should_preserve_main_source_bytes(shared: Any, expected: str):
    assert render_container_default([shared, shared]) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=" + expected + ")"
    )


def test__render_model__repeated_tuple_in_dict__should_preserve_main_source_bytes():
    shared = (1, 2)
    assert render_container_default({"a": shared, "b": shared}) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default={'a': (1, 2), 'b': (1, 2)})"
    )


class TupleValues(Enum):
    pair = (1, 2)
    nested = ((1, 2), "a")
    singleton = (1,)
    empty = ()


def test__render_model__tuple_enum_values__should_preserve_main_source_bytes():
    assert render_model(TupleValues, VersionBundle(Version("2000-01-01")), "2000-01-01") == (
        "class TupleValues(Enum):\n    pair = (1, 2)\n    nested = ((1, 2), 'a')\n    singleton = (1,)\n    empty = ()"
    )


@pytest.mark.parametrize("container_type", [list, dict])
def test__render_model__recursive_default__should_fail_with_render_error(container_type: type):
    default = container_type()
    if isinstance(default, list):
        default.append(default)
    else:
        default["self"] = default

    with pytest.raises(CadwynRenderError, match=r"recursive.*default_factory"):
        render_container_default(default)


def test__render_model__shared_container_in_default__should_fail_instead_of_duplicating_it():
    shared: list[int] = []
    with pytest.raises(CadwynRenderError, match=r"shared.*default_factory"):
        render_container_default([shared, shared])


def test__render_model__default_with_cycle_through_namedtuple__should_fail_with_render_error():
    values: list[Any] = []
    point = RecursivePoint(values)
    values.append(point)
    with pytest.raises(CadwynRenderError, match=r"recursive.*default_factory"):
        render_container_default(point)


@pytest.mark.parametrize("container_type", [Counter, OrderedDict])
def test__render_model__container_default_with_attributes__should_fail_instead_of_losing_them(container_type: type):
    default = container_type()
    default.label = "keep"

    class ModelWithStatefulDefault(BaseModel):
        value: Any = default

    with pytest.raises(CadwynRenderError, match=r"attributes.*default_factory"):
        render_model(ModelWithStatefulDefault, VersionBundle(Version("2000-01-01")), "2000-01-01")


@pytest.mark.parametrize("factory", [lambda: 1, partial(int, 1)])
def test__render_model__defaultdict_with_unsupported_factory__should_fail_with_render_error(factory: Any):
    with pytest.raises(CadwynRenderError, match=r"factory.*default_factory"):
        render_container_default(defaultdict(factory))


def test__render_model__with_weird_types():
    result = render_model_by_path(
        "tests._resources.render.complex.classes:ModelWithWeirdFields",
        "tests._resources.render.complex.versions:app",
        "2000-01-01",
    )
    rendered_lambda = "lambda: 91"

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


class ModelWithAliases(BaseModel):
    name: str = Field(alias="a")
    code: str = Field(alias="c", validation_alias="cv", serialization_alias="cs")


@pytest.mark.parametrize(
    ("change_field", "expected_fields"),
    [
        pytest.param(
            lambda field: field.had(alias="legacy"),
            """
    name: str = Field(alias='legacy', validation_alias='legacy', serialization_alias='legacy')
    code: str = Field(alias='legacy', validation_alias='cv', serialization_alias='cs')
""",
            id="had",
        ),
        pytest.param(
            lambda field: field.didnt_have("alias"),
            """
    name: str = Field()
    code: str = Field(validation_alias='cv', serialization_alias='cs')
""",
            id="didnt_have",
        ),
    ],
)
def test__render_model__alias_instruction__should_render_aliases_that_follow_alias(
    change_field: Any, expected_fields: str
):
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                change_field(schema(ModelWithAliases).field("name")),
                change_field(schema(ModelWithAliases).field("code")),
            ),
        ),
        Version("2000-01-01"),
    )

    assert code(render_model(ModelWithAliases, versions, "2000-01-01")) == code(
        "\nclass ModelWithAliases(BaseModel):" + expected_fields
    )


class ModelWithAliasGenerator(BaseModel):
    model_config = ConfigDict(alias_generator=str.upper)
    name: str = Field(default="default", alias="a")
    unchanged: str = Field(default="default")


@pytest.mark.parametrize(
    ("instruction", "expected_fields", "key_of_name"),
    [
        # `cadwyn render` doesn't render model_config, so it passes the aliases from the generator explicitly
        pytest.param(
            schema(ModelWithAliasGenerator).field("name").didnt_have("alias"),
            """
    name: str = Field(default='default', alias='NAME')
    unchanged: str = Field(default='default')
""",
            "NAME",
            id="alias_instruction",
        ),
        pytest.param(
            schema(ModelWithAliasGenerator).field("unchanged").had(description="Old"),
            """
    name: str = Field(default='default', alias='a', validation_alias='a', serialization_alias='a')
    unchanged: str = Field(default='default', description='Old')
""",
            "a",
            id="other_instruction",
        ),
    ],
)
def test__render_model__model_with_alias_generator__should_render_aliases_of_fields_whose_alias_changed(
    instruction: Any, expected_fields: str, key_of_name: str
):
    versions = VersionBundle(Version("2001-01-01", version_change(instruction)), Version("2000-01-01"))

    rendered = render_model(ModelWithAliasGenerator, versions, "2000-01-01")

    assert code(rendered) == code("\nclass ModelWithAliasGenerator(BaseModel):" + expected_fields)
    namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field}
    exec(rendered, namespace)
    assert namespace["ModelWithAliasGenerator"].model_validate({key_of_name: "x"}).name == "x"
