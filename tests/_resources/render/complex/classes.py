from enum import Enum, auto
from typing import Annotated, Literal, Union

from annotated_types import Interval
from pydantic import BaseModel, Field, StringConstraints


class AlmostEmptyEnum(Enum):
    foo = auto()


class MyEnum(Enum):
    foo = auto()
    baz = auto()


class A(BaseModel):
    daz: int


def my_default_factory():
    raise NotImplementedError


class ModelWithWeirdFields(A):
    """My docstring"""

    foo: dict = Field(default={"a": "b"})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: Literal[MyEnum.foo]
    saz: Annotated[str, StringConstraints(to_upper=True)]
    laz: Annotated[int, None, Interval(gt=12), None] = Field(gt=12)
    taz: Union[int, str, None] = Field(default_factory=lambda: 83)  # pragma: no branch
    naz: list[int] = Field(default=[1, 2, 3])
