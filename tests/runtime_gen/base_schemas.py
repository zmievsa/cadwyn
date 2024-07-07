from enum import Enum

from pydantic import BaseModel


class EmptySchema(BaseModel):
    pass


class MyEnum(Enum):
    foo = 83
