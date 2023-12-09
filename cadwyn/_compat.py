from typing import Any, TypeAlias

ModelField: TypeAlias = Any
Undefined: TypeAlias = Any

try:
    from pydantic.fields import FieldInfo, ModelField, Undefined

    PYDANTIC_V2 = False
except ImportError:
    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined

    PYDANTIC_V2 = True

    ModelField = FieldInfo
    Undefined = PydanticUndefined
