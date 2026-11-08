"""Generics that share a TypeVar with typevar_namespace_second whose quoted bound resolves to another Item here."""

from typing import Generic

from pydantic import BaseModel

from tests._data.typevar_namespace_second import Page as OtherPage
from tests._data.typevar_namespace_second import T


class Item(BaseModel):
    a: str


class Page(BaseModel, Generic[T]):
    item: T


class Envelope(BaseModel, Generic[T]):
    page: OtherPage[T]


class EnvelopeWithItem(BaseModel, Generic[T]):
    page: OtherPage[T]
    item: T
