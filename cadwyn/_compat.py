import ast
import dataclasses
import inspect
from typing import Any, TypeAlias

from fastapi._compat import ModelField as FastAPIModelField
from pydantic import BaseModel, Field

ModelField: TypeAlias = Any  # pyright: ignore[reportGeneralTypeIssues]
Undefined: TypeAlias = Any

try:
    PYDANTIC_V2 = False

    from pydantic.fields import FieldInfo, ModelField, Undefined  # pyright: ignore # noqa: PGH003

    _all_field_arg_names = []
    EXTRA_FIELD_NAME = "extra"
except ImportError:
    PYDANTIC_V2 = True

    from pydantic.fields import FieldInfo
    from pydantic_core import PydanticUndefined

    ModelField: TypeAlias = FieldInfo  # pyright: ignore # noqa: PGH003
    Undefined = PydanticUndefined  # pyright: ignore # noqa: PGH003
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


@dataclasses.dataclass(slots=True)
class PydanticFieldWrapper:
    annotation: Any

    init_model_field: dataclasses.InitVar[ModelField]  # pyright: ignore[reportGeneralTypeIssues]
    field_info: FieldInfo = dataclasses.field(init=False)

    annotation_ast: ast.expr | None = None
    field_ast: ast.expr | None = None

    def __post_init__(self, init_model_field: ModelField):  # pyright: ignore[reportGeneralTypeIssues]
        if isinstance(init_model_field, FieldInfo):
            self.field_info = init_model_field
        else:
            self.field_info = init_model_field.field_info

    def get_annotation_for_rendering(self):
        if self.annotation_ast:
            return self.annotation_ast
        else:
            return self.annotation

    def update_attribute(self, *, name: str, value: Any):
        if PYDANTIC_V2:
            if name in FieldInfo.metadata_lookup:
                self.field_info.metadata.extend(FieldInfo._collect_metadata({name: value}))
            self.field_info._attributes_set[name] = value
        else:
            setattr(self.field_info, name, value)

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
