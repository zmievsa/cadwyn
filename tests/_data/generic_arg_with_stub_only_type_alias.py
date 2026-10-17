"""A generic arg whose native type alias fails when its value is evaluated at runtime. Only loads on Python 3.12+"""

from typing import TYPE_CHECKING, Any, ClassVar, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


if TYPE_CHECKING:

    class StubOnlyGeneric(Generic[T]):
        """Like pandas.Series or Django's QuerySet, which can only be subscripted by type checkers"""

else:

    class StubOnlyGeneric:
        pass


if TYPE_CHECKING:
    StubOnlyAlias = Any
else:
    # The values of native type aliases are evaluated lazily
    exec("type StubOnlyAlias = StubOnlyGeneric[int]")


class Page(BaseModel, Generic[T]):
    # A default makes it a statement that runs, unlike lazily evaluated annotations
    items: list[T] = Field(default_factory=list)


class Other(BaseModel):
    value: int = 1


class Item(BaseModel):
    name: str = "a"
    registry: ClassVar[Optional[StubOnlyAlias]] = None
