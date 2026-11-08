import re
import struct
import sys
from collections import Counter, OrderedDict, defaultdict, deque, namedtuple
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from enum import Enum
from functools import partial
from typing import Annotated, Any, ClassVar, NamedTuple, Self
from uuid import UUID

import pytest
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing_extensions import override

from cadwyn import VersionChange, schema
from cadwyn._render import render_model, render_model_by_path, render_module_by_path
from cadwyn.exceptions import CadwynRenderError, ImportFromStringError
from cadwyn.schema_generation import generate_versioned_models
from cadwyn.structure.versions import Version, VersionBundle
from tests._resources.render.config_helpers import add_legacy_marker
from tests.conftest import version_change
from tests.test_cli import code


class Point(NamedTuple):
    x: int
    y: int


class RecursivePoint(NamedTuple):
    values: list[Any]


EmptyPoint = namedtuple("EmptyPoint", ())  # noqa: PYI024 # Cover collections.namedtuple as well as typing.NamedTuple


class PointSubclass(namedtuple("PointBase", "x y")):  # noqa: PYI024 # Cover subclasses of collections.namedtuple
    __slots__ = ()


class IterablePoint(namedtuple("PointBase", "x y")):  # noqa: PYI024
    __slots__ = ()

    def __new__(cls, values: Any) -> Self:
        return super().__new__(cls, *values)

    def __getnewargs__(self) -> tuple[tuple[Any, ...]]:
        return (tuple(self),)

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}({tuple.__repr__(self)})"


class RedeclaredPoint(IterablePoint):
    __slots__ = ()
    _fields = PointSubclass._fields


class SwappedIterPoint(PointSubclass):
    __slots__ = ()

    @override
    def __iter__(self) -> Iterator[Any]:
        return iter((self.y, self.x))


class StatefulPoint(PointSubclass):
    label: str


class SwappingPoint(NamedTuple):
    x: Any
    y: Any

    @override
    def __iter__(self) -> Iterator[Any]:
        return iter((self.y, self.x))

    @override
    def __getitem__(self, index: Any) -> Any:
        return tuple.__getitem__(self, 1 - index)


class PairCounter(Counter):
    def __init__(self, pairs: Iterable[tuple[str, Any]] = ()) -> None:
        super().__init__()
        for key, count in pairs:
            self[key] = count

    @override
    def __repr__(self) -> str:
        return f"PairCounter({list(self.items())!r})"

    @override
    def __reduce__(self) -> Any:
        return (type(self), (list(self.items()),))


class InitCounter(Counter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class SlotCounter(Counter):
    __slots__ = ("label",)
    label: str


class IncrementingPoint(namedtuple("PointBase", "x y")):  # noqa: PYI024
    __slots__ = ()

    def __new__(cls, x: int, y: int) -> Self:
        return super().__new__(cls, x + 1, y)

    @override
    def __repr__(self) -> str:
        return f"IncrementingPoint({self.x - 1!r}, {self.y!r})"


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
    namespace["datetime"] = sys.modules["datetime"]
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


class AnyPoint(NamedTuple):
    x: Any
    y: Any


class MyCounter(Counter):
    pass


class MyDefaultDict(defaultdict):
    pass


class MyInt(int):
    pass


class MyStr(str):
    __slots__ = ()


class SlotList(list):
    __slots__ = ("tag",)
    tag: str

    @override
    def __repr__(self) -> str:
        return f"SlotList({list.__repr__(self)})"


def test__render_model__namedtuple_with_nested_subclass__should_keep_none_value_and_type():
    default = AnyPoint(RepresentationList([None]), 2)
    actual = evaluate_rendered_default(default)
    assert actual == default
    assert type(actual) is AnyPoint
    assert type(actual.x) is RepresentationList
    assert actual.x[0] is None


def test__render_model__counter_default__should_keep_the_order_of_counts():
    rendered_default = evaluate_rendered_default(Counter({"a": 1, "b": 3, "c": 1.0}))
    assert type(rendered_default) is Counter
    assert list(rendered_default.items()) == [("a", 1), ("b", 3), ("c", 1.0)]
    assert type(rendered_default["c"]) is float


def test__render_model__defaultdict_subclass__should_wrap_constructor_error_with_field_context():
    with pytest.raises(CadwynRenderError, match=r"ModelWithContainerDefault.value.*MyDefaultDict"):
        render_container_default(MyDefaultDict(int, a=1))


@pytest.mark.parametrize(("default", "source", "base"), [(MyInt(1), "1", int), (MyStr("x"), "'x'", str)])
def test__render_model__scalar_subclass__should_render_base_type(default: Any, source: str, base: type):
    assert render_container_default(default) == (
        f"class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default={source})"
    )
    actual = evaluate_rendered_default(default)
    assert actual == default
    assert type(actual) is base


@pytest.mark.parametrize(
    ("value", "expression"),
    [(float("inf"), "float('inf')"), (float("-inf"), "float('-inf')"), (float("nan"), "float('nan')")],
)
@pytest.mark.parametrize(
    ("wrap", "extract"),
    [
        (lambda value: value, lambda value: value),
        (lambda value: AnyPoint(value, 2), lambda value: value.x),
        (lambda value: defaultdict(int, a=value), lambda value: value["a"]),
        (lambda value: Counter(a=value), lambda value: value["a"]),
        (lambda value: RepresentationList([value]), lambda value: value[0]),
    ],
)
def test__render_model__nonfinite_default__should_emit_executable_float_expression(
    value: float, expression: str, wrap: Any, extract: Any
):
    default = wrap(value)
    assert expression in render_container_default(default)
    actual = extract(evaluate_rendered_default(default))
    assert type(actual) is float
    assert struct.pack("!d", actual) == struct.pack("!d", value)


def test__render_model__finite_float_default__should_render_its_repr():
    assert render_container_default(-0.0) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=-0.0)"
    )


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        (RepresentationList([date(2020, 1, 1)]), "RepresentationList([datetime.date(2020, 1, 1)])"),
        (AnyPoint(date(2020, 1, 1), 2), "AnyPoint(datetime.date(2020, 1, 1), 2)"),
        (
            defaultdict(list, a=[UUID(int=1)]),
            "defaultdict(list, {'a': [UUID('00000000-0000-0000-0000-000000000001')]})",
        ),
        (IterablePoint([1, date(2020, 1, 1)]), "IterablePoint((1, datetime.date(2020, 1, 1)))"),
        (RedeclaredPoint([1, 2]), "RedeclaredPoint((1, 2))"),
        (PairCounter([("a", 1), ("b", 3)]), "PairCounter([('a', 1), ('b', 3)])"),
    ],
)
def test__render_model__container_with_qualified_or_custom_items__should_render_reprs_of_items(
    default: Any, expected: str
):
    assert render_container_default(default) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=" + expected + ")"
    )
    rendered_default = evaluate_rendered_default(default)
    assert type(rendered_default) is type(default)
    assert rendered_default == default


class ModelWithSwappingPointDefault(BaseModel):
    # Instructions deep-copy their defaults, and deepcopy reads named tuples through their own __iter__
    value: Any = SwappingPoint(1, [None, int])


def test__render_model__namedtuple_with_own_item_access__should_render_its_stored_fields():
    default = ModelWithSwappingPointDefault.model_fields["value"].default
    assert list(default) == [[None, int], 1]
    assert default[0] == [None, int]
    rendered = render_model(ModelWithSwappingPointDefault, VersionBundle(Version("2000-01-01")), "2000-01-01")
    assert rendered == (
        "class ModelWithSwappingPointDefault(BaseModel):\n    value: Any = Field(default=SwappingPoint(1, [None, int]))"
    )
    namespace: dict[str, Any] = dict(globals())
    exec(rendered, namespace)
    actual = namespace["ModelWithSwappingPointDefault"].model_fields["value"].default
    assert type(actual) is SwappingPoint
    assert tuple.__getitem__(actual, 0) == 1
    assert tuple.__getitem__(actual, 1) == [None, int]


class DequeBackedList(list):
    def __new__(cls, items: Iterable[Any] = ()) -> Any:
        return deque(items)


def make_deque_backed_list() -> DequeBackedList:
    default = list.__new__(DequeBackedList)
    list.__init__(default, [1, 2])
    return default


class ModelWithDequeBackedListDefault(BaseModel):
    # Instructions deep-copy their defaults, which would already turn this one into a deque
    value: Any = make_deque_backed_list()


def test__render_model__subclass_whose_constructor_returns_another_type__should_fail_instead_of_changing_it():
    with pytest.raises(CadwynRenderError, match=r"ModelWithDequeBackedListDefault.value.*DequeBackedList.*type"):
        render_model(ModelWithDequeBackedListDefault, VersionBundle(Version("2000-01-01")), "2000-01-01")


def make_slot_counter() -> "SlotCounter":
    counter = SlotCounter(a=1)
    counter.label = "keep"
    return counter


def make_stateful_point() -> PointSubclass:
    point = StatefulPoint(1, 2)
    point.label = "keep"
    return point


@pytest.mark.parametrize(
    "make_default",
    [
        lambda: PointSubclass(1, 2),
        lambda: IncrementingPoint(1, 2),
        lambda: SwappedIterPoint(1, 2),
        make_stateful_point,
        lambda: MyCounter(a=3),
        lambda: InitCounter({"a": 1, "b": 1.0}),
        make_slot_counter,
    ],
)
def test__render_model__subclass_whose_constructor_cannot_rebuild_it__should_fail_instead_of_changing_it(
    make_default: Any,
):
    default = make_default()
    with pytest.raises(CadwynRenderError, match=rf"ModelWithContainerDefault.value.*{type(default).__name__}"):
        render_container_default(default)


class SameRepr:
    @override
    def __repr__(self) -> str:
        return "SameRepr()"


@pytest.mark.parametrize(
    "make_default",
    [
        lambda: {SameRepr(), SameRepr()},
        lambda: frozenset({SameRepr(), SameRepr()}),
        lambda: {SameRepr(): 1, SameRepr(): 2},
        lambda: OrderedDict([(SameRepr(), 1), (SameRepr(), 2)]),
        lambda: defaultdict(int, {SameRepr(): 1, SameRepr(): 2}),
        lambda: Counter({SameRepr(): 1, SameRepr(): 2}),
        lambda: MyCounter({SameRepr(): 1, SameRepr(): 2}),
    ],
)
def test__render_model__distinct_items_with_identical_reprs__should_fail_instead_of_merging_them(make_default: Any):
    with pytest.raises(CadwynRenderError, match=r"ModelWithContainerDefault.value.*identical representations"):
        render_container_default(make_default())


@pytest.mark.parametrize(
    ("value", "expression"),
    [
        (complex(float("inf"), 0), "complex(float('inf'), 0.0)"),
        (complex(0, float("-inf")), "complex(0.0, float('-inf'))"),
        (complex(float("nan"), -0.0), "complex(float('nan'), -0.0)"),
    ],
)
def test__render_model__nonfinite_complex_default__should_emit_executable_complex_expression(
    value: complex, expression: str
):
    assert render_container_default(value) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=" + expression + ")"
    )
    actual = evaluate_rendered_default(value)
    assert type(actual) is complex
    assert struct.pack("!dd", actual.real, actual.imag) == struct.pack("!dd", value.real, value.imag)


def test__render_model__finite_complex_default__should_render_its_repr():
    assert render_container_default(1 - 2j) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=1 - 2j)"
    )


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


def test__render_model__namedtuple_with_shared_reference__should_preserve_mutable_identity():
    actual = evaluate_rendered_default(AnyPoint(REFERENCED_LIST, REFERENCED_LIST))
    assert actual.x is actual.y is REFERENCED_LIST


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        (CustomSet({1}), "CustomSet({1})"),
        (CustomFrozenSet({1}), "CustomFrozenSet({1})"),
        (RepresentationList([1, 2]), "RepresentationList([1, 2])"),
        (SlotList([1]), "SlotList([1])"),
        (CopyingList([[1], [2], [3]]), "CopyingList([[1], [2], [3]])"),
    ],
)
def test__render_model__reconstructible_subclass__should_render_its_repr(default: Any, expected: str):
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
def test__render_model__repeated_immutable_container__should_render_each_occurrence(shared: Any, expected: str):
    assert render_container_default([shared, shared]) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default=" + expected + ")"
    )


def test__render_model__repeated_tuple_in_dict__should_render_each_occurrence():
    shared = (1, 2)
    assert render_container_default({"a": shared, "b": shared}) == (
        "class ModelWithContainerDefault(BaseModel):\n    value: Any = Field(default={'a': (1, 2), 'b': (1, 2)})"
    )


class TupleValues(Enum):
    pair = (1, 2)
    nested = ((1, 2), "a")
    singleton = (1,)
    empty = ()


def test__render_model__tuple_enum_values__should_render_tuple_literals():
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


class RenderedConfig(BaseModel):
    model_config = ConfigDict(title="New title")
    value: str


def test__render_model__schema_config_patch__should_render_older_config():
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(RenderedConfig).had(config=ConfigDict(extra="forbid")))),
        Version("2000-01-01"),
    )
    namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field}
    exec(render_model(RenderedConfig, versions, "2000-01-01"), namespace)
    rendered = namespace["RenderedConfig"]
    generated = generate_versioned_models(versions)["2000-01-01"][RenderedConfig]
    assert rendered.model_config == generated.model_config
    assert rendered.model_json_schema() == generated.model_json_schema()


class CamelConfig(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)
    some_field: int


def test__render_model__config_change_with_imported_alias_generator__should_render_generator_by_name():
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(CamelConfig).had(config=ConfigDict(title="Old")))),
        Version("2000-01-01"),
    )
    rendered = render_model(CamelConfig, versions, "2000-01-01")
    assert "'alias_generator': to_camel" in rendered
    namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field, "to_camel": to_camel}
    exec(rendered, namespace)
    generated = generate_versioned_models(versions)["2000-01-01"][CamelConfig]
    assert namespace["CamelConfig"].model_json_schema() == generated.model_json_schema()


def test__render_model__config_function_from_helper_module__should_render_function_by_name():
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(schema(RenderedConfig).had(config=ConfigDict(json_schema_extra=add_legacy_marker))),
        ),
        Version("2000-01-01"),
    )
    rendered = render_model(RenderedConfig, versions, "2000-01-01")
    assert "'json_schema_extra': add_legacy_marker" in rendered
    namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field, "add_legacy_marker": add_legacy_marker}
    exec(rendered, namespace)
    assert namespace["RenderedConfig"].model_json_schema()["x-legacy"] is True


legacy = False
LEGACY_EXTRA = lambda s: s.update({"x-legacy": True})  # noqa: E731


def _render_config(config: ConfigDict) -> str:
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(RenderedConfig).had(config=config))),
        Version("2000-01-01"),
    )
    return render_model(RenderedConfig, versions, "2000-01-01")


@pytest.mark.parametrize(
    "config",
    [
        ConfigDict(json_schema_extra=lambda s: s.update({"x-legacy": True})),
        {"json_schema_extra": lambda s: s.update({"x-legacy": True})},
        ConfigDict(json_schema_extra=lambda s: s.update({"x-legacy": all(bool(flag) for flag in [not legacy])})),
    ],
)
def test__render_model__config_lambda__should_inline_its_source(config: ConfigDict):
    namespace: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field, **globals()}
    exec(_render_config(config), namespace)
    assert namespace["RenderedConfig"].model_json_schema()["x-legacy"] is True


@pytest.mark.parametrize(
    ("config", "error"),
    [
        (ConfigDict(json_schema_extra=eval('lambda s: s.update({"x-legacy": True})')), "could not get source"),
        (ConfigDict(json_schema_extra=LEGACY_EXTRA), "No lambda found in json_schema_extra"),
        (ConfigDict(json_schema_extra=lambda s, _m, marker=True: s.update({"x-legacy": marker})), "defaults"),
        (ConfigDict(json_schema_extra=lambda s, _m, *, marker=True: s.update({"x-legacy": marker})), "defaults"),
        (ConfigDict(json_schema_extra=lambda s: s.update({"x-title": RenderedConfig.__name__})), "'RenderedConfig'"),
        ({"json_schema_extra": len}, "invalid syntax"),
    ],
)
def test__render_model__config_callable_that_cannot_be_rendered__should_name_model_and_key(
    config: ConfigDict, error: str
):
    with pytest.raises(CadwynRenderError, match=rf"RenderedConfig\.json_schema_extra: .*{error}"):
        _render_config(config)


def test__render_model__config_lambda_with_closure__should_raise_error():
    marker = {"x-legacy": True}
    with pytest.raises(CadwynRenderError, match=r"RenderedConfig\.json_schema_extra: .*captured variables"):
        _render_config(ConfigDict(json_schema_extra=lambda s: s.update(marker)))


def test__render_model__config_lambda_with_other_globals__should_raise_error():
    from types import FunctionType

    prototype = ConfigDict(json_schema_extra=lambda s: s.update({"x-legacy": legacy}))["json_schema_extra"]
    assert isinstance(prototype, FunctionType)
    callback = FunctionType(prototype.__code__, prototype.__globals__ | {"legacy": True})
    with pytest.raises(CadwynRenderError, match=r"RenderedConfig\.json_schema_extra: .*'legacy'.*different object"):
        _render_config(ConfigDict(json_schema_extra=callback))


OldRenderedConfig = str


def test__render_model__config_lambda_using_name_of_renamed_model__should_raise_error():
    extra = ConfigDict(json_schema_extra=lambda s: s.update({"x-title": OldRenderedConfig.__name__}))
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(RenderedConfig).had(name="OldRenderedConfig", config=extra))),
        Version("2000-01-01"),
    )
    assert generate_versioned_models(versions)["2000-01-01"][RenderedConfig].model_json_schema()["x-title"] == "str"
    with pytest.raises(CadwynRenderError, match=r"OldRenderedConfig\.json_schema_extra: .*'OldRenderedConfig'"):
        render_model(RenderedConfig, versions, "2000-01-01")


def test__render_model__config_function_without_module_binding__should_raise_error():
    def local_extra(s: dict[str, Any]) -> None:
        raise NotImplementedError

    with pytest.raises(CadwynRenderError, match=r"RenderedConfig\.json_schema_extra: 'local_extra'.*different object"):
        _render_config(ConfigDict(json_schema_extra=local_extra))
