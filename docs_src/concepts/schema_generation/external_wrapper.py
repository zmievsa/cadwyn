from typing import Generic, TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT")


class Page(BaseModel, Generic[SchemaT]):
    items: list[SchemaT]
    total: int
