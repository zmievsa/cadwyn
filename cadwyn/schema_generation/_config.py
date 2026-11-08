import inspect
from typing import (
    TYPE_CHECKING,
    Union,
)

import pydantic
from pydantic import BaseModel
from pydantic._internal._config import ConfigWrapper

from cadwyn._utils import (
    is_same_value,
)
from cadwyn.exceptions import InvalidGenerationInstructionError

if TYPE_CHECKING:
    from ._models import (
        _PydanticModelWrapper,
    )


def _change_model_config(
    model: "_PydanticModelWrapper[BaseModel]",
    config_patch: pydantic.ConfigDict,
    version_change_name: str,
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
) -> None:
    config = model.other_attributes["model_config"] | model.get_config_changes(schemas)
    current = ConfigWrapper(config.copy(), check=False)
    # Pydantic <2.14 fills in the validation settings here; newer versions keep an effective config separately.
    current.core_config(None)
    if {"populate_by_name", "validate_by_alias", "validate_by_name"} & config_patch.keys():
        config.update(validate_by_alias=current.validate_by_alias, validate_by_name=current.validate_by_name)
    if "populate_by_name" in config_patch:
        # On older Pydantic releases these flags were copied into model_config at class creation. A patch of
        # populate_by_name must update them too, while letting explicitly patched validation flags take precedence.
        config.update(validate_by_alias=True, validate_by_name=config_patch["populate_by_name"])
    config.update(config_patch)
    updated = ConfigWrapper(config, check=False)
    updated.core_config(None)
    for key in config_patch:
        if is_same_value(getattr(current, key), getattr(updated, key)):
            raise InvalidGenerationInstructionError(
                f'You tried to change config of "{model.name}", key "{key}", in "{version_change_name}" '
                "but it already has the value you tried to assign."
            )
    model.other_attributes["model_config"] = config
    model.config_changed_keys |= frozenset(config_patch)
    if {"populate_by_name", "validate_by_alias", "validate_by_name"} & config_patch.keys():
        model.config_changed_keys |= {"validate_by_alias", "validate_by_name"}


def _get_description(docstring: Union[str, None]) -> Union[str, None]:
    """Return the description that pydantic generates from the docstring of a model"""
    return inspect.cleandoc(docstring) if docstring else None
