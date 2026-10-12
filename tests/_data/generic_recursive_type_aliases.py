"""A model that uses a recursive type alias, defined in the way that the tests set in `KIND` before loading this module.

Native type aliases (`type JsonData = ...`) can only be defined since Python 3.12 and refer to themselves directly,
while typing_extensions.TypeAliasType refers to itself through forward refs.
"""

from typing import TYPE_CHECKING, Any, Generic, TypeVar, Union

from pydantic import BaseModel
from typing_extensions import TypeAliasType

T = TypeVar("T")
# The tests set it before executing this module
KIND: str = globals()["KIND"]


class Leaf(BaseModel):
    value: int = 1


class Other(BaseModel):
    value: int = 1


if TYPE_CHECKING:
    JsonData = Any
elif KIND == "native":
    exec("type JsonData = dict[str, JsonData] | list[JsonData] | Leaf | str | int | None")
else:
    JsonData = TypeAliasType("JsonData", Union[dict[str, "JsonData"], list["JsonData"], Leaf, str, int, None])


class Page(BaseModel, Generic[T]):
    items: list[T]


class Item(BaseModel):
    data: JsonData
