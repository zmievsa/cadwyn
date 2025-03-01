from collections.abc import Callable
from dataclasses import dataclass
from typing import ParamSpec, TypeAlias, TypeVar

from pydantic import BaseModel

VersionedModel = BaseModel
VersionType: TypeAlias = str
_P = ParamSpec("_P")
_R = TypeVar("_R")
Endpoint: TypeAlias = Callable[_P, _R]


@dataclass(slots=True, kw_only=True)
class _HiddenAttributeMixin:
    is_hidden_from_changelog: bool = False
