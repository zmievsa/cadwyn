"""Models whose forward ref is only defined after Cadwyn has looked at them, which can close a cycle through a generic.

The tests load a fresh copy of this module for every case because defining `Later` changes these models.
"""

from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")
# Only declared here so that define_later() can define it
Later: type[BaseModel]


class Page(BaseModel, Generic[T]):
    items: list[T]


class Box(BaseModel, Generic[T]):
    value: Optional[T] = None


class Other(BaseModel):
    value: int = 1


class X(BaseModel):
    other: Optional[Other] = None
    later: Optional[Later] = None  # ty: ignore[invalid-type-form]


class W(BaseModel):
    x: Optional[X] = None
    page: Optional[Page[X]] = None
    # A plain self reference makes Box[W] keep its head args after looking at X and Page[X]
    w: Optional[W] = None


def define_later(*, refer_back_to_page: bool) -> None:
    global Later

    if refer_back_to_page:

        class Later(BaseModel):
            back: Optional[Page[X]] = None

    else:

        class Later(BaseModel):
            back: Optional[int] = None
