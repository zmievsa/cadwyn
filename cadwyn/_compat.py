from typing import Any, TypeAlias

ModelField: TypeAlias = Any
Undefined: TypeAlias = Any

try:
    from pydantic.fields import FieldInfo, ModelField, Undefined
except ImportError:
    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined

    ModelField = FieldInfo
    Undefined = PydanticUndefined
