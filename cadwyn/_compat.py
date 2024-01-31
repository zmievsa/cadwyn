import ast
import dataclasses
import inspect
from typing import Any, TypeAlias

import pydantic
from fastapi._compat import ModelField as FastAPIModelField
from pydantic import BaseModel, Field

ModelField: TypeAlias = Any  # pyright: ignore[reportGeneralTypeIssues]
PydanticUndefined: TypeAlias = Any
VALIDATOR_CONFIG_KEY = "__validators__"

try:
    PYDANTIC_V2 = False

    from pydantic.fields import FieldInfo, ModelField  # pyright: ignore # noqa: PGH003
    from pydantic.fields import Undefined as PydanticUndefined  # pyright: ignore # noqa: PGH003

    _all_field_arg_names = []
    EXTRA_FIELD_NAME = "extra"
except ImportError:
    PYDANTIC_V2 = True

    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined  # pyright: ignore # noqa: PGH003

    ModelField: TypeAlias = FieldInfo  # pyright: ignore # noqa: PGH003
    _all_field_arg_names = sorted(
        [
            name
            for name, param in inspect.signature(Field).parameters.items()
            if param.kind in {inspect._ParameterKind.KEYWORD_ONLY, inspect._ParameterKind.POSITIONAL_OR_KEYWORD}
        ],
    )
    EXTRA_FIELD_NAME = "json_schema_extra"


_empty_field_info = Field()
dict_of_empty_field_info = {k: getattr(_empty_field_info, k) for k in FieldInfo.__slots__}


def is_pydantic_1_constrained_type(value: object):
    """This method only works for pydanticV1. It is always False in PydanticV2"""
    return isinstance(value, type) and value.__name__.startswith("Constrained") and value.__name__.endswith("Value")


def is_constrained_type(value: object):
    if PYDANTIC_V2:
        import annotated_types

        return isinstance(value, annotated_types.Len | annotated_types.Interval | pydantic.StringConstraints)

    else:
        return is_pydantic_1_constrained_type(value)


@dataclasses.dataclass(slots=True)
class PydanticFieldWrapper:
    """We DO NOT maintain field.metadata at all"""

    annotation: Any

    init_model_field: dataclasses.InitVar[ModelField]  # pyright: ignore[reportGeneralTypeIssues]
    field_info: FieldInfo = dataclasses.field(init=False)

    annotation_ast: ast.expr | None = None
    # In the expressions "foo: str | None = None" and "foo: str | None = Field(default=None)"
    # the value_ast is "None" and "Field(default=None)" respectively
    value_ast: ast.expr | None = None

    def __post_init__(self, init_model_field: ModelField):  # pyright: ignore[reportGeneralTypeIssues]
        if isinstance(init_model_field, FieldInfo):
            self.field_info = init_model_field
        else:
            self.field_info = init_model_field.field_info

    def update_attribute(self, *, name: str, value: Any):
        if PYDANTIC_V2:
            self.field_info._attributes_set[name] = value
        else:
            setattr(self.field_info, name, value)

    def delete_attribute(self, *, name: str) -> None:
        if PYDANTIC_V2:
            self.field_info._attributes_set.pop(name)
        else:
            setattr(self.field_info, name, PydanticUndefined)

    @property
    def passed_field_attributes(self):
        if PYDANTIC_V2:
            attributes = {
                attr_name: self.field_info._attributes_set[attr_name]
                for attr_name in _all_field_arg_names
                if attr_name in self.field_info._attributes_set
            }
            # PydanticV2 always adds frozen to _attributes_set but we don't want it if it wasn't explicitly set
            if attributes.get("frozen", ...) is None:
                attributes.pop("frozen")
            return attributes

        else:
            attributes = {
                attr_name: attr_val
                for attr_name, default_attr_val in dict_of_empty_field_info.items()
                if attr_name != EXTRA_FIELD_NAME
                and (attr_val := getattr(self.field_info, attr_name)) != default_attr_val
            }
            extras = getattr(self.field_info, EXTRA_FIELD_NAME) or {}
            return attributes | extras


def model_fields(model: type[BaseModel]) -> dict[str, FieldInfo]:
    if PYDANTIC_V2:
        return model.model_fields
    else:
        return model.__fields__  # pyright: ignore[reportDeprecated]


def model_dump(model: BaseModel, by_alias: bool = False, exclude_unset: bool = False) -> dict[str, Any]:
    if PYDANTIC_V2:
        return model.model_dump(by_alias=by_alias, exclude_unset=exclude_unset)
    else:
        return model.dict(by_alias=by_alias, exclude_unset=exclude_unset)  # pyright: ignore[reportDeprecated]


def rebuild_fastapi_body_param(old_body_param: FastAPIModelField, new_body_param_type: type[BaseModel]):
    kwargs: dict[str, Any] = {"name": old_body_param.name, "field_info": old_body_param.field_info}
    if PYDANTIC_V2:
        old_body_param.field_info.annotation = new_body_param_type
        kwargs.update({"mode": old_body_param.mode})
    else:
        kwargs.update(
            {
                "type_": new_body_param_type,
                "class_validators": old_body_param.class_validators,  # pyright: ignore[reportGeneralTypeIssues]
                "default": old_body_param.default,
                "required": old_body_param.required,
                "model_config": old_body_param.model_config,  # pyright: ignore[reportGeneralTypeIssues]
                "alias": old_body_param.alias,
            },
        )
    return FastAPIModelField(**kwargs)
