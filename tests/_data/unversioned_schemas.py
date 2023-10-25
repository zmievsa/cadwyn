from pydantic import BaseModel, Field

from cadwyn import internal_body_representation_of

from .latest import SchemaWithInternalRepresentation


class UnversionedSchema3(BaseModel):
    baz: int


@internal_body_representation_of(SchemaWithInternalRepresentation)
class InternalSchema(SchemaWithInternalRepresentation):
    bar: str | None = Field(default=None)
