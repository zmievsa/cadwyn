from pydantic import BaseModel

from . import SchemaWithOneFloatField


class MySchema(BaseModel):
    foo: int


class SchemaThatDependsOnAnotherSchema(BaseModel):
    foo: SchemaWithOneFloatField
    bar: int
