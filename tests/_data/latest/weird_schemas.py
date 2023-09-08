from enum import Enum, auto
from typing import Literal

from pydantic import BaseModel, Field


def my_default_factory():
    raise NotImplementedError


class MyEnum(Enum):
    baz = auto()


class ModelWithWeirdFields(BaseModel):
    foo: dict = Field(default={"a": "b"})
    bar: list[int] = Field(default_factory=my_default_factory)
    baz: Literal[MyEnum.baz]
