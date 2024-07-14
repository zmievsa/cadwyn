import re
import sys
from datetime import date
from enum import Enum

import pytest

from cadwyn._render import render_model, render_model_by_path, render_module_by_path
from cadwyn.exceptions import ImportFromStringError
from cadwyn.structure.versions import Version, VersionBundle
from tests.test_cli import code


def test__render_model__with_weird_types():
    result = render_model_by_path(
        "tests._resources.render.complex.classes:ModelWithWeirdFields",
        "tests._resources.render.complex.versions:app",
        "2000-01-01",
    )
    # TODO: sobolevn has created a tool for doing such nocovers in a better manner.
    # hopefully someday we will switch to it.
    if sys.version_info >= (3, 11):  # pragma: no cover # We cover this in CI
        rendered_lambda = "lambda: 83"
    else:  # pragma: no cover # We cover this in CI
        rendered_lambda = "lambda : 83"

    # TODO: As you see, we do not rename bases correctly in render. We gotta fix it some day...
    assert code(result) == code(
        f'''
class ModelWithWeirdFields(A):
    """My docstring"""
    foo: dict = Field(default={{'a': 'b'}})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: typing.Literal[MyEnum.foo] = Field()
    saz: Annotated[str, StringConstraints(to_upper=True)] = Field()
    laz: Annotated[int, None, Interval(gt=12, ge=None, lt=None, le=None), None] = Field()
    taz: typing.Union[int, str, None] = Field(default_factory={rendered_lambda})
    naz: list[int] = Field(default=[1, 2, 3])
    gaz: Annotated[bytes, Strict(strict=True), Len(min_length=0, max_length=None)] = Field(min_length=3, title='Hewwo')
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
    versions = VersionBundle(Version(date(2000, 1, 1)))

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
