from decimal import Decimal
from enum import Enum, auto
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, conint, constr
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


# TODO: If you don't type hint AND specify PrivateAttr for private fields -- pydantic v1 (maybe v2 too)
# won't know they exist. Make a note of this in docs
class SchemaWithPrivateAttr(BaseModel):
    _private_attr: str = PrivateAttr(default="hewwo")


class AnyRequestSchema(BaseModel):
    __root__: Any


class AnyResponseSchema(BaseModel):
    __root__: Any


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
        return SchemaWithOneFloatField(foo=3.14)


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


CONINT_LT = 10
ANOTHER_VAR, CONINT_LT_ALIAS = 11, CONINT_LT


class SchemaWithConstraints(BaseModel):
    foo: conint(lt=CONINT_LT_ALIAS)  # pyright: ignore[reportGeneralTypeIssues]
    bar: str = Field(max_length=CONINT_LT_ALIAS)


class SchemaWithSpecialConstraints(BaseModel):
    foo: constr(to_upper=True)  # pyright: ignore[reportGeneralTypeIssues]


class SchemaWithInternalRepresentation(BaseModel):
    foo: int


"Nothing to see here. Move along."
