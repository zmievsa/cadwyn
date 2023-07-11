from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeAlias, TypeVar

from pydantic import BaseModel

VersionedModel = BaseModel
VersionDate = str
_P = ParamSpec("_P")
_R = TypeVar("_R")
Endpoint: TypeAlias = Callable[_P, Awaitable[_R]]
