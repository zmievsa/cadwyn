from typing import Any, TypeAlias

from pydantic import Field

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

_empty_field_info = Field()
dict_of_empty_field_info = {k: getattr(_empty_field_info, k) for k in FieldInfo.__slots__}


def is_pydantic_constrained_type(value: object):
    return isinstance(value, type) and value.__name__.startswith("Constrained") and value.__name__.endswith("Value")


def get_attrs_that_are_not_from_field_and_that_are_from_field(value: type):
    parent_public_attrs = {k: v for k, v in value.mro()[1].__dict__.items() if not k.startswith("_")}
    value_private_attrs = {k: v for k, v in value.__dict__.items() if not k.startswith("_")}
    attrs_in_value_different_from_parent = {
        k: v for k, v in value_private_attrs.items() if k in parent_public_attrs and parent_public_attrs[k] != v
    }
    attrs_in_value_different_from_parent_that_are_not_in_field_def = {
        k: v for k, v in attrs_in_value_different_from_parent.items() if k not in dict_of_empty_field_info
    }
    attrs_in_value_different_from_parent_that_are_in_field_def = {
        k: v for k, v in attrs_in_value_different_from_parent.items() if k in dict_of_empty_field_info
    }

    return (
        attrs_in_value_different_from_parent_that_are_not_in_field_def,
        attrs_in_value_different_from_parent_that_are_in_field_def,
    )
