from enum import Enum
from typing import (
    Any,
    Union,
    cast,
)

import pydantic
from pydantic import BaseModel, RootModel
from typing_extensions import (
    TypeVar,
)

from cadwyn._utils import (
    is_sqlalchemy_model,
    lenient_issubclass,
)
from cadwyn.exceptions import SchemaGenerationError

_T_ANY_MODEL = TypeVar("_T_ANY_MODEL", bound=Union[BaseModel, Enum])
_PYDANTIC_ALL_EXPORTED_NAMES = set(pydantic.__all__)
_DEFAULT_PYDANTIC_CLASSES = (BaseModel, RootModel)

try:
    from pydantic_settings import BaseSettings

    _DEFAULT_PYDANTIC_CLASSES = (*_DEFAULT_PYDANTIC_CLASSES, BaseSettings)
except ImportError:  # pragma: no cover
    pass


def _get_deep_copy_error(description: str, error: Exception) -> SchemaGenerationError:
    return SchemaGenerationError(
        f"Cadwyn cannot generate the versioned copy of {description} because it cannot deep copy it "
        f"({type(error).__name__}: {error}). Every version gets its own deep copy of it, as pydantic gives every "
        "instance its own copy of a mutable default. Please make it deep-copyable, e.g. with __deepcopy__."
    )


def _unwrap_model(model: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
    while hasattr(model, "__cadwyn_original_model__"):
        model = cast("type[_T_ANY_MODEL]", model.__cadwyn_original_model__)
    return model


def _is_dunder(attr_name: str):
    return attr_name.startswith("__") and attr_name.endswith("__")


def _is_versionable_model(model: Any) -> bool:
    return (
        isinstance(model, type)
        and lenient_issubclass(model, (BaseModel, Enum))
        and model not in _DEFAULT_PYDANTIC_CLASSES
        and not is_sqlalchemy_model(model)
    )
