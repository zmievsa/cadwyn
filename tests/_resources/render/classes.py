from enum import Enum, auto

from pydantic import BaseModel


class MyEnum(Enum):
    foo = auto()


class A(BaseModel):
    foo: str
