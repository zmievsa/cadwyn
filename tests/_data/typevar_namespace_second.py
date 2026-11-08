"""A generic whose TypeVar bound is quoted, so it resolves to this module's Item (see typevar_namespace_first)."""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound="Item")


class Item(BaseModel):
    b: str


class Page(BaseModel, Generic[T]):
    item: T
