import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, ParamSpec, TypeAlias, TypeVar, Union

from pydantic import BaseModel

VersionedModel = BaseModel
VersionT = TypeVar("VersionT", bound=Union[int, datetime.date])


class VersionType(Generic[VersionT]):
    __slots__ = ("value",)

    def __init__(self, value: VersionT) -> None:
        super().__init__()
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VersionType) and self.value == other.value

    def __lt__(self, other: object) -> bool:
        return isinstance(other, VersionType) and self.value < other.value


_P = ParamSpec("_P")
_R = TypeVar("_R")
Endpoint: TypeAlias = Callable[_P, _R]


@dataclass(slots=True, kw_only=True)
class _HiddenAttributeMixin:
    is_hidden_from_changelog: bool = False
