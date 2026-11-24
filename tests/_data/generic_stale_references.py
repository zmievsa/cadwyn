"""Models that refer to `Later`, a name that is only defined when the tests call define_later().

`X` refers to `Later` in the way that the tests set in `KIND` before loading this module. The tests load a fresh
copy of this module for every case because defining `Later` changes these models.
"""

from typing import TYPE_CHECKING, ClassVar, Generic, Optional, TypeVar, Union

from pydantic import BaseModel, Field

T = TypeVar("T")
KIND: str = globals()["KIND"]

if TYPE_CHECKING:

    class Later(BaseModel):
        back: Optional[object] = None

    LaterAlias = Optional[Later]


class Page(BaseModel, Generic[T]):
    items: list[T]


class Box(BaseModel, Generic[T]):
    value: Optional[T] = None


class Ref(BaseModel, Generic[T]):
    id: int = 1


class Other(BaseModel):
    value: int = 1


RefOfLater = Ref[Union[Other, "Later"]]


if KIND == "lazy_default_factory":

    def make_later_id() -> Later:
        return 1  # ty: ignore[invalid-explicit-return]

else:

    def make_later_id() -> "Later":
        return 1  # ty: ignore[invalid-explicit-return]


class X(BaseModel):
    other: Optional[Other] = None
    if KIND == "field":
        later: "Optional[Later]" = None
    elif KIND == "generic_arg":
        later: Ref["Later"] = Ref["Later"]()
    elif KIND in {"default_factory", "lazy_default_factory"}:
        later_id: object = Field(default_factory=make_later_id)
    elif KIND == "classvar":
        later: ClassVar[Later]
    elif KIND == "type_alias":
        exec("type LaterAlias = Optional[Later]")
        later: Optional[LaterAlias] = None
    else:
        exec("type LaterAlias = Later")
        adapter: ClassVar[LaterAlias]


class W(BaseModel):
    x: Optional[X] = None
    page: Optional[Page[X]] = None
    ref_of_later: Optional[RefOfLater] = None
    w: "Optional[W]" = None


def define_later(*, refer_back_to_page: bool, refer_back_to_ref_of_later: bool = False) -> None:
    global Later

    if refer_back_to_ref_of_later:

        class Later(BaseModel):
            back: Optional[RefOfLater] = None

    elif refer_back_to_page:

        class Later(BaseModel):
            back: Optional[Page[X]] = None

    else:

        class Later(BaseModel):
            back: Optional[int] = None
