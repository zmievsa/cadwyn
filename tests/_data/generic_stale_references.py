"""Models that refer to `Later`, a name that is only defined when the tests call define_later().

`X` refers to `Later` in the way that the tests set in `KIND` before loading this module. The tests load a fresh
copy of this module for every case because defining `Later` changes these models.
"""

from typing import TYPE_CHECKING, ClassVar, Generic, Optional, TypeVar, Union

from pydantic import BaseModel, Field

T = TypeVar("T")
# The tests set it before executing this module
KIND: str = globals()["KIND"]

if TYPE_CHECKING:
    # Only defined by define_later()
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


# Its fields do not use its type param so pydantic can resolve them while its own arg refers to an undefined name
RefOfLater = Ref[Union[Other, "Later"]]


if KIND == "lazy_default_factory":
    # Only evaluated lazily since Python 3.14
    def make_later_id() -> Later:
        return 1  # ty: ignore[invalid-return-type]

else:

    def make_later_id() -> "Later":
        return 1  # ty: ignore[invalid-return-type]


class X(BaseModel):
    other: Optional[Other] = None
    if KIND == "field":
        later: "Optional[Later]" = None
    elif KIND == "generic_arg":
        later: Ref["Later"] = Ref["Later"]()
    elif KIND in {"default_factory", "lazy_default_factory"}:
        later_id: int = Field(default_factory=make_later_id)
    elif KIND == "classvar":
        # Only evaluated lazily since Python 3.14
        later: ClassVar[Later]
    elif KIND == "type_alias":
        # The values of type aliases are evaluated lazily since Python 3.12
        exec("type LaterAlias = Optional[Later]")
        later: Optional[LaterAlias] = None
    else:
        exec("type LaterAlias = Later")
        adapter: ClassVar[LaterAlias]


class W(BaseModel):
    x: Optional[X] = None
    page: Optional[Page[X]] = None
    ref_of_later: Optional[RefOfLater] = None
    # A plain self reference makes Box[W] keep its head args after looking at X and Page[X]
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
