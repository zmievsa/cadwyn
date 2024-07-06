import ast
import dataclasses
import functools
import inspect
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Generic, TypeAlias, TypeVar

import pydantic
from issubclass import issubclass
from pydantic import (
    BaseModel,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
    root_validator,
    validator,
)
from pydantic._internal import _decorators
from pydantic._internal._decorators import (
    FieldSerializerDecoratorInfo,
    FieldValidatorDecoratorInfo,
    ModelSerializerDecoratorInfo,
    ModelValidatorDecoratorInfo,
    RootValidatorDecoratorInfo,
    ValidatorDecoratorInfo,
)
from pydantic.fields import ComputedFieldInfo
from typing_extensions import Doc, Self

from cadwyn._compat import (
    PYDANTIC_V2,
    FieldInfo,
    dict_of_empty_field_info,
)

if TYPE_CHECKING:
    from cadwyn.codegen._common import _FieldName

_T_MODEL = TypeVar("_T_MODEL", bound=type[BaseModel | Enum])
_T_PYDANTIC_MODEL = TypeVar("_T_PYDANTIC_MODEL", bound=BaseModel)

ModelField: TypeAlias = Any  # pyright: ignore[reportRedeclaration]
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

    init_model_field: dataclasses.InitVar[ModelField]

    annotation: Any = dataclasses.field(init=False)
    passed_field_attributes: dict[str, Any] = dataclasses.field(init=False)

    def __post_init__(self, init_model_field: ModelField):
        if isinstance(init_model_field, FieldInfo):
            field_info = init_model_field
            self.annotation = init_model_field.annotation
        else:
            field_info = init_model_field.field_info
            self.annotation = init_model_field.field_info.annotation
        self.passed_field_attributes = _extract_passed_field_attributes(field_info)

    def update_attribute(self, *, name: str, value: Any):
        self.passed_field_attributes[name] = value

    def delete_attribute(self, *, name: str) -> None:
        self.passed_field_attributes.pop(name)

    def generate_field_copy(self) -> pydantic.fields.FieldInfo:
        return pydantic.Field(**self.passed_field_attributes)


def _extract_passed_field_attributes(field_info):
    if PYDANTIC_V2:
        attributes = {
            attr_name: field_info._attributes_set[attr_name]
            for attr_name in _all_field_arg_names
            if attr_name in field_info._attributes_set
        }
        # PydanticV2 always adds frozen to _attributes_set but we don't want it if it wasn't explicitly set
        if attributes.get("frozen", ...) is None:
            attributes.pop("frozen")
        return attributes

    else:
        attributes = {
            attr_name: attr_val
            for attr_name, default_attr_val in dict_of_empty_field_info.items()
            if attr_name != EXTRA_FIELD_NAME and (attr_val := getattr(field_info, attr_name)) != default_attr_val
        }
        extras = getattr(field_info, EXTRA_FIELD_NAME) or {}
        return attributes | extras


@dataclasses.dataclass(slots=True, kw_only=True)
class _ValidatorWrapper:
    kwargs: dict[str, Any]
    func: Callable
    decorator: Callable
    is_deleted: bool = False


@dataclasses.dataclass(slots=True, kw_only=True)
class _PerFieldValidatorWrapper(_ValidatorWrapper):
    fields: list[str]


@dataclasses.dataclass(slots=True)
class _PydanticRuntimeModelWrapper(Generic[_T_PYDANTIC_MODEL]):
    cls: type[_T_PYDANTIC_MODEL]
    name: str
    doc: str | None
    fields: Annotated[
        dict["_FieldName", PydanticFieldWrapper],
        Doc(
            "Fields that belong to this model, not to its parents. I.e. The ones that were either defined or overriden "
        ),
    ]
    validators: dict[str, _PerFieldValidatorWrapper | _ValidatorWrapper]
    other_attributes: dict[str, Any]
    annotations: dict[str, Any] = dataclasses.field(init=False, repr=False)
    _parents: list[Self] | None = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        self.annotations = self.cls.__annotations__.copy()

    def _get_parents(self, schemas: "dict[type, Self]"):
        if self._parents is not None:
            return self._parents
        parents = []
        for base in self.cls.mro()[1:]:
            if base in schemas:
                parents.append(schemas[base])
            elif issubclass(base, BaseModel):
                parents.append(wrap_pydantic_model(base))
        self._parents = parents
        return parents

    def _get_defined_fields_through_mro(self, schemas: "dict[type, Self]") -> dict[str, PydanticFieldWrapper]:
        fields = {}

        for parent in reversed(self._get_parents(schemas)):
            fields |= parent.fields

        return fields | self.fields

    def _get_defined_annotations_through_mro(self, schemas: "dict[type, Self]") -> dict[str, Any]:
        annotations = {}

        for parent in reversed(self._get_parents(schemas)):
            annotations |= parent.annotations

        return annotations | self.annotations

    def generate_model_copy(self) -> type[_T_PYDANTIC_MODEL]:
        per_field_validators = {
            name: validator.decorator(*validator.fields, **validator.kwargs)(validator.func)
            for name, validator in self.validators.items()
            if not validator.is_deleted and type(validator) == _PerFieldValidatorWrapper
        }
        root_validators = {
            name: validator.decorator(**validator.kwargs)(validator.func)
            for name, validator in self.validators.items()
            if not validator.is_deleted and type(validator) == _ValidatorWrapper
        }
        fields = {name: field.generate_field_copy() for name, field in self.fields.items()}
        return type(self.cls)(
            self.name,
            self.cls.__bases__,
            self.other_attributes
            | per_field_validators
            | root_validators
            | fields
            | {"__annotations__": self.annotations, "__doc__": self.doc},
        )


# TODO: Add a test where we delete a parent field for which we have a child validator. To handle this correctly, we have to first initialize the parents, and only then the children. Or maybe even process validator changes AFTER the migrations have been done.
@functools.cache
def wrap_pydantic_model(model: type[_T_PYDANTIC_MODEL]) -> _PydanticRuntimeModelWrapper[_T_PYDANTIC_MODEL]:
    defined_names = _get_all_class_attributes(model)
    decorators = [
        *model.__pydantic_decorators__.validators.values(),
        *model.__pydantic_decorators__.field_validators.values(),
        *model.__pydantic_decorators__.root_validators.values(),
        *model.__pydantic_decorators__.field_serializers.values(),
        *model.__pydantic_decorators__.model_serializers.values(),
        *model.__pydantic_decorators__.model_validators.values(),
        *model.__pydantic_decorators__.computed_fields.values(),
    ]
    validators = {}
    for decorator_wrapper in decorators:
        if defined_names:
            # This is a fix for cases when this validator overrides a field name from parent class
            if decorator_wrapper.cls_var_name not in defined_names:
                continue
        elif decorator_wrapper.cls_var_name not in model.__dict__:
            continue

        wrapped_validator = _wrap_validator(decorator_wrapper.func, decorator_wrapper.shim, decorator_wrapper.info)
        validators[decorator_wrapper.cls_var_name] = wrapped_validator
    fields = {
        field_name: PydanticFieldWrapper(model.model_fields[field_name]) for field_name in model.__annotations__.keys()
    }

    main_attributes = fields | validators
    other_attributes = {
        attr_name: attr_val
        for attr_name, attr_val in model.__dict__.items()
        if attr_name not in main_attributes
        and (
            (defined_names and attr_name in defined_names)
            or not (_is_dunder(attr_name) or attr_name in {"_abc_impl", "model_fields", "model_computed_fields"})
        )
    }
    other_attributes |= {
        "model_config": model.model_config,
        "__module__": model.__module__,
        "__qualname__": model.__qualname__,
    }
    return _PydanticRuntimeModelWrapper(
        model,
        name=model.__name__,
        doc=model.__doc__,
        fields=fields,
        other_attributes=other_attributes,
        validators=validators,
    )


def _wrap_validator(func: Callable, is_pydantic_v1_style_validator: Any, decorator_info: _decorators.DecoratorInfo):
    # This is only for pydantic v1 style validators
    if is_pydantic_v1_style_validator and func.__closure__:
        func = func.__closure__[0].cell_contents
    if inspect.ismethod(func):
        func = func.__func__
    kwargs = dataclasses.asdict(decorator_info)
    decorator_fields = kwargs.pop("fields", None)
    actual_decorator = PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP[type(decorator_info)]
    if is_pydantic_v1_style_validator:
        # There's an inconsistency in their interfaces so we gotta resort to this
        mode = kwargs.pop("mode", "after")
        kwargs["pre"] = mode != "after"
    if decorator_fields is not None:
        return _PerFieldValidatorWrapper(
            func=func, fields=list(decorator_fields), decorator=actual_decorator, kwargs=kwargs
        )
    else:
        return _ValidatorWrapper(func=func, decorator=actual_decorator, kwargs=kwargs)


def _is_dunder(attr_name):
    return attr_name.startswith("__") and attr_name.endswith("__")


def _get_all_class_attributes(cls: type) -> set[str]:
    try:
        source = inspect.getsource(cls)
    except OSError:
        return set()

    cls_ast = ast.parse(source).body[0]
    if not isinstance(cls_ast, ast.ClassDef):
        return set()

    defined_names = set()
    for node in cls_ast.body:
        defined_names.update(_get_names_defined_in_node(node))
    return defined_names


def _get_names_defined_in_node(node: ast.stmt):
    defined_names = set()

    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        defined_names.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                defined_names.add(target.id)
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            defined_names.add(node.target.id)

    return defined_names


PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP = {
    ValidatorDecoratorInfo: validator,
    FieldValidatorDecoratorInfo: field_validator,
    FieldSerializerDecoratorInfo: field_serializer,
    RootValidatorDecoratorInfo: root_validator,
    ModelValidatorDecoratorInfo: model_validator,
    ModelSerializerDecoratorInfo: model_serializer,
    ComputedFieldInfo: computed_field,
}