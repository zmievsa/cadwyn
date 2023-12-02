import datetime
from collections.abc import Callable
from typing import ParamSpec, TypeAlias, TypeVar

from pydantic import BaseModel

VersionedModel = BaseModel
VersionDate = datetime.date
_P = ParamSpec("_P")
_R = TypeVar("_R")
Endpoint: TypeAlias = Callable[_P, _R]
