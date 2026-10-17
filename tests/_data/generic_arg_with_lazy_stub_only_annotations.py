"""A generic arg whose default factory has a lazily evaluated annotation that fails at runtime. Only loads on 3.14+"""

from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


if TYPE_CHECKING:

    class StubOnlyGeneric(Generic[T]):
        """Like pandas.Series or Django's QuerySet, which can only be subscripted by type checkers"""

else:

    class StubOnlyGeneric:
        pass


def make_tags() -> StubOnlyGeneric[int]:
    return []  # ty: ignore[invalid-return-type]


class Page(BaseModel, Generic[T]):
    # A default makes it a statement that runs, unlike lazily evaluated annotations
    items: list[T] = Field(default_factory=list)


class Other(BaseModel):
    value: int = 1


class Item(BaseModel):
    name: str = "a"
    tags: Any = Field(default_factory=make_tags)
