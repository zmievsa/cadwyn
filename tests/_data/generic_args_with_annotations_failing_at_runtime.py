"""Generic args whose annotations fail when evaluated at runtime, e.g. generics that only exist in stubs.

`KIND` is set by the tests before this module is executed. Main never evaluates these annotations because
it never copies the models in generic args.
"""

from __future__ import annotations

import types  # noqa: TC003
from typing import TYPE_CHECKING, Any, Generic, Optional, TypeVar

from fastapi import Depends
from pydantic import BaseModel, Field

T = TypeVar("T")
KIND: str = globals()["KIND"]


if TYPE_CHECKING:

    class StubOnlyGeneric(Generic[T]):
        """Like pandas.Series or Django's QuerySet, which can only be subscripted by type checkers"""

else:

    class StubOnlyGeneric:
        pass


def make_tags_subscripting_stub_only_generic() -> StubOnlyGeneric[int]:
    return []  # ty: ignore[invalid-explicit-return]


def make_tags_with_missing_attribute() -> types.DoesNotExistAtRuntime:  # ty: ignore[missing-attribute]
    return []


def dependency_with_stub_only_annotation(value: StubOnlyGeneric[int]) -> int:  # pragma: no cover
    return 0


def count_with_dependency(value: int = Depends(dependency_with_stub_only_annotation)) -> int:
    del value
    return 0


class Page(BaseModel, Generic[T]):
    items: list[T]


class Ref(BaseModel, Generic[T]):
    id: int = 1


class Other(BaseModel):
    value: int = 1


if KIND == "default_factory_type_error":

    class Item(BaseModel):
        name: str = "a"
        tags: Any = Field(default_factory=make_tags_subscripting_stub_only_generic)

elif KIND == "default_factory_attribute_error":

    class Item(BaseModel):
        name: str = "a"
        tags: Any = Field(default_factory=make_tags_with_missing_attribute)

elif KIND == "dependency_of_default_factory":

    class Item(BaseModel):
        name: str = "a"
        count: int = Field(default_factory=count_with_dependency)

else:

    class Item(BaseModel):
        name: str = "a"
        ref: Optional[Ref["StubOnlyGeneric[int]"]] = None  # noqa: UP037
