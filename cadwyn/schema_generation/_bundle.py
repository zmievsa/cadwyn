import collections.abc
import copy
import dataclasses
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
)

from pydantic import BaseModel

from cadwyn._utils import (
    DATACLASS_KW_ONLY,
    DATACLASS_SLOTS,
)
from cadwyn.structure.versions import HeadVersion, Version  # noqa: TC001 # Needed by runtime annotation introspection.

if TYPE_CHECKING:
    from ._enums import (
        _EnumWrapper,
    )
    from ._models import (
        _PydanticModelWrapper,
    )


@dataclasses.dataclass(**DATACLASS_SLOTS)
class _ModelBundle:
    enums: dict[type[Enum], "_EnumWrapper[Enum]"]
    schemas: dict[type[BaseModel], "_PydanticModelWrapper[BaseModel]"]
    models_with_config_instructions: frozenset[type[BaseModel]]

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ModelBundle":
        memos_of_models: dict[type[BaseModel], dict[int, Any]] = collections.defaultdict(dict)
        result = _ModelBundle(
            enums=copy.deepcopy(self.enums, memo),
            schemas={
                model: wrapper.copy_with_schemas(self.schemas, memos_of_models)
                for model, wrapper in self.schemas.items()
            },
            models_with_config_instructions=self.models_with_config_instructions,
        )
        memo[id(self)] = result
        return result


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _RuntimeSchemaGenContext:
    current_version: "Union[Version, HeadVersion]"
    models: _ModelBundle
