import dataclasses
import inspect
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeAlias, TypeVar

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
from pydantic._internal._decorators import (
    FieldSerializerDecoratorInfo,
    FieldValidatorDecoratorInfo,
    ModelSerializerDecoratorInfo,
    ModelValidatorDecoratorInfo,
    RootValidatorDecoratorInfo,
    ValidatorDecoratorInfo,
)
from pydantic.fields import ComputedFieldInfo
from typing_extensions import Self

from cadwyn._compat import (
    PYDANTIC_V2,
    FieldInfo,
    dict_of_empty_field_info,
)

if TYPE_CHECKING:
    from cadwyn.codegen._common import _FieldName

_T_MODEL = TypeVar("_T_MODEL", bound=type[BaseModel | Enum])

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

    field_info: FieldInfo = dataclasses.field(init=False, repr=False)
    annotation: FieldInfo = dataclasses.field(init=False)
    passed_field_attributes: dict[str, Any] = dataclasses.field(init=False)

    def __post_init__(self, init_model_field: ModelField):
        if isinstance(init_model_field, FieldInfo):
            self.field_info = init_model_field
            self.annotation = init_model_field.annotation
        else:
            self.field_info = init_model_field.field_info
            self.annotation = init_model_field.field_info.annotation
        self.passed_field_attributes = _extract_passed_field_attributes(self.field_info)

    def update_attribute(self, *, name: str, value: Any):
        self.passed_field_attributes[name] = value

    def delete_attribute(self, *, name: str) -> None:
        self.passed_field_attributes.pop(name)


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


def _extract_fields_and_validators_from_model(model: BaseModel):
    fields = {field_name: PydanticFieldWrapper(field) for field_name, field in model.__fields__.items()}
    validators = model.__pydantic_validator__


@dataclasses.dataclass(slots=True, kw_only=True)
class _ValidatorWrapper:
    kwargs: dict[str, Any]
    func: Callable
    is_deleted: bool = False  # TODO: Maybe remove is_deleted?


@dataclasses.dataclass(slots=True, kw_only=True)
class _PerFieldValidatorWrapper(_ValidatorWrapper):
    fields: list[str]


@dataclasses.dataclass(slots=True)
class _PydanticRuntimeModelWrapper:
    cls: type[BaseModel]
    name: str
    fields: "dict[_FieldName, PydanticFieldWrapper]"
    per_field_validators: list[_PerFieldValidatorWrapper]
    root_validators: list[_ValidatorWrapper]
    methods: list[Callable]
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
                fields, validators = get_fields_and_validators_from_model(base)
                parents.append(type(self)(base, base.__name__, fields, validators))
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


class A(BaseModel):
    doo: int
    bar: str

    @validator("doo", "bar")
    @classmethod
    def gar(cls, value):
        return value


class Schema(A, extra="ignore"):
    foo: str

    @validator("foo", "doo")
    @classmethod
    def bar(cls, value):
        return value

    @field_validator("foo", "doo")
    @classmethod
    def baz(cls, value):
        return value


# TODO: Add a test where we delete a parent field for which we have a child validator. To handle this correctly, we have to first initialize the parents, and only then the children. Or maybe even process validator changes AFTER the migrations have been done.
def get_validators(model: type[BaseModel]):
    from pydantic._internal._core_utils import get_type_ref

    print(model.__fields__)
    model_type_ref = get_type_ref(model)
    print(model.__pydantic_decorators__.validators["bar"])
    print(model.__pydantic_decorators__.field_validators["baz"])

    extras = model.__pydantic_extra__ or {}
    decorators = [
        *model.__pydantic_decorators__.validators.values(),
        *model.__pydantic_decorators__.field_validators.values(),
        *model.__pydantic_decorators__.root_validators.values(),
        *model.__pydantic_decorators__.field_serializers.values(),
        *model.__pydantic_decorators__.model_serializers.values(),
        *model.__pydantic_decorators__.model_validators.values(),
        *model.__pydantic_decorators__.computed_fields.values(),
    ]
    root_validators = []
    per_field_validators = []
    model_type_ref = get_type_ref(model)
    for decorator in decorators:
        if decorator.cls_ref != model_type_ref:
            continue
        kwargs = decorator.info.__dataclass_fields__.copy()
        fields = kwargs.pop("fields", None)
        if fields is not None:
            per_field_validators.append(
                _PerFieldValidatorWrapper(func=decorator.func, fields=list(fields), kwargs=kwargs)
            )
        else:
            root_validators.append(_ValidatorWrapper(func=decorator.func, kwargs=kwargs))

    _PydanticRuntimeModelWrapper(
        model, model.__name__, ..., per_field_validators=per_field_validators, root_validators=root_validators
    )

    type(model)(model.__name__, model.__bases__, dict_, **extras)


PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP = {
    ValidatorDecoratorInfo: validator,
    FieldValidatorDecoratorInfo: field_validator,
    FieldSerializerDecoratorInfo: field_serializer,
    RootValidatorDecoratorInfo: root_validator,
    ModelValidatorDecoratorInfo: model_validator,
    ModelSerializerDecoratorInfo: model_serializer,
    ComputedFieldInfo: computed_field,
}

PYDANTIC_PER_FIELD_DECORATORS = {
    ValidatorDecoratorInfo,
    FieldValidatorDecoratorInfo,
    FieldSerializerDecoratorInfo,
    FieldSerializerDecoratorInfo,
}

# get_validators(Schema)


def copy_pydantic_model(model: type[BaseModel]):
    pass
