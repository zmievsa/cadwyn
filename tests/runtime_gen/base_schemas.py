from enum import Enum

from pydantic import BaseModel


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str


class MyEnum(Enum):
    foo = 83
