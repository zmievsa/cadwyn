from decimal import Decimal
from enum import Enum, auto

from pydantic import BaseModel, Field
from pydantic import Field as PydanticField


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


class EmptySchemaWithArbitraryTypesAllowed(BaseModel, arbitrary_types_allowed=True):
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


class SchemaThatDependsOnAnotherSchema(SchemaWithOneFloatField):
    foo: SchemaWithOneFloatField
    bat: SchemaWithOneFloatField | int = Field(default=SchemaWithOneFloatField(foo=3.14))

    def baz(self, daz: SchemaWithOneFloatField) -> SchemaWithOneFloatField:
        return SchemaWithOneFloatField(foo=3.14)  # pragma: no cover


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


class SchemaWithExtras(BaseModel):
    foo: str = Field(lulz="foo")
