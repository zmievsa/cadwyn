from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from typing_extensions import ParamSpec, TypeAlias, TypeVar

from cadwyn._utils import DATACLASS_KW_ONLY, DATACLASS_SLOTS

VersionedModel = BaseModel
VersionType: TypeAlias = str
_P = ParamSpec("_P")
_R = TypeVar("_R")
Endpoint: TypeAlias = Callable[_P, _R]


@dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _HiddenAttributeMixin:
    is_hidden_from_changelog: bool
