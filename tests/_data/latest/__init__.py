from decimal import Decimal
from enum import Enum, auto

from pydantic import BaseModel
from pydantic import Field as PydanticField

from universi import Field


class StrEnum(str, Enum):
    a = auto()


class EnumWithOneMember(Enum):
    a = auto()


class EnumWithTwoMembers(Enum):
    a = auto()
    b = auto()


class EmptyEnum(Enum):
    pass


class EmptySchema(BaseModel):
    pass


class SchemaWithOneStrField(BaseModel):
    foo: str = Field(default="foo")


class SchemaWithOneIntField(BaseModel):
    """Hello darkness"""

    foo: int


class SchemaWithOneDecimalField(BaseModel):
    foo: Decimal


class SchemaWithOneListOfIntField(BaseModel):
    foo: list[int]


class SchemaWithOneFloatField(BaseModel):
    foo: float


class SchemaWithOnePydanticField(BaseModel):
    foo: SchemaWithOneIntField


class SchemaWithWrongFieldConstructor(BaseModel):
    foo: str = PydanticField(default="bar")


class NonPydanticSchema:
    foo: int


class SchemaThatOverridesField(SchemaWithOneIntField):
    foo: bytes


class SchemaWithUnionFields(BaseModel):
    foo: int | str
    bar: EmptySchema | None
