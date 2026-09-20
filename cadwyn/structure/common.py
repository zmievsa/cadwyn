from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from pydantic import BaseModel
from typing_extensions import ParamSpec, TypeVar

from cadwyn._utils import DATACLASS_KW_ONLY, DATACLASS_SLOTS, is_sqlalchemy_model
from cadwyn.exceptions import CadwynStructureError

VersionedModel = BaseModel
VersionType: TypeAlias = str
_P = ParamSpec("_P")
_R = TypeVar("_R")
Endpoint: TypeAlias = Callable[_P, _R]


@dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _HiddenAttributeMixin:
    is_hidden_from_changelog: bool


def _validate_schema_is_versionable(model: type) -> None:
    if is_sqlalchemy_model(model):
        raise CadwynStructureError(
            f"SQLAlchemy class '{model.__name__}' cannot be migrated. "
            "Use a separate Pydantic model for versioned request and response schemas."
        )
