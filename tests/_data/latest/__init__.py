from decimal import Decimal
from enum import Enum, auto
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr
from pydantic import Field as PydanticField, conint

from universi.fields import FillablePrivateAttr, FillablePrivateAttrMixin


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


# TODO: If you don't type hint the private fields -- pydantic v1 (maybe v2 too) won't know they exist.
# Make a note of this in docs
class SchemaWithPrivateAttrs(FillablePrivateAttrMixin, BaseModel):
    _non_fillable_attr: str = PrivateAttr(default="hewwo")
    _fillable_attr: str = FillablePrivateAttr()


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


CONINT_LT = 10
ANOTHER_VAR, CONINT_LT_ALIAS = 11, CONINT_LT


class SchemaWithConstrainedInt(BaseModel):
    foo: conint(lt=CONINT_LT_ALIAS)


"Nothing to see here. Move along."
