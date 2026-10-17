"""Models whose forward refs only pydantic can resolve and that form cycles through generics.

The tests load a fresh copy of this module for every case because rebuilding these models changes them.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Ref(BaseModel, Generic[T]):
    id: int


class Page(BaseModel, Generic[T]):
    items: list[T]


class Changed(BaseModel):
    name: str = Field(alias="fullName")


class NestedUnionA(BaseModel):
    class Detail(BaseModel):
        label: str

    changed: Changed
    # Detail is only available through the namespace of the class
    peer: Detail | Ref[NestedUnionB]


class NestedUnionB(BaseModel):
    other: Ref[NestedUnionA]


def make_detail(model: Any) -> MetadataDetail:
    return MetadataDetail(label=str(model(id=1).id))


class MetadataA(BaseModel):
    changed: Changed
    # Ref["MetadataB"] is only reachable through the default factory in the metadata of an unresolved annotation
    detail: Annotated[MetadataDetail, Field(default_factory=functools.partial(make_detail, model=Ref["MetadataB"]))]


class MetadataB(BaseModel):
    other: Ref[MetadataA]


class MetadataDetail(BaseModel):
    label: str
