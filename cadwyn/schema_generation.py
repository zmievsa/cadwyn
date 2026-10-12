import collections.abc
import copy
import dataclasses
import functools
import inspect
import sys
import threading
import types
import typing
from collections.abc import Callable, Sequence
from datetime import date
from enum import Enum
from functools import cache
from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
    ForwardRef,
    Generic,
    NamedTuple,
    TypeAlias,
    TypeGuard,
    Union,
    cast,
    get_args,
    get_origin,
)

import fastapi.params
import fastapi.security.base
import fastapi.utils
import pydantic
import pydantic._internal._decorators
from fastapi import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, RootModel
from pydantic._internal import _decorators
from pydantic._internal._config import ConfigWrapper
from pydantic._internal._decorators import (
    FieldSerializerDecoratorInfo,
    FieldValidatorDecoratorInfo,
    ModelSerializerDecoratorInfo,
    ModelValidatorDecoratorInfo,
    RootValidatorDecoratorInfo,
    ValidatorDecoratorInfo,
)
from pydantic._internal._fields import rebuild_model_fields
from pydantic._internal._generics import PydanticGenericMetadata, get_model_typevars_map
from pydantic._internal._known_annotated_metadata import collect_known_metadata
from pydantic._internal._model_construction import unpack_lenient_weakvaluedict
from pydantic._internal._namespace_utils import NsResolver
from pydantic._internal._typing_extra import try_eval_type as pydantic_try_eval_type
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import (
    Any,
    Doc,
    TypeVar,
    _AnnotatedAlias,
    assert_never,
    final,
    overload,
    override,
)

from cadwyn._asts import GenericAliasUnionArgs
from cadwyn._utils import (
    DATACLASS_KW_ONLY,
    DATACLASS_SLOTS,
    Sentinel,
    UnionType,
    fully_unwrap_decorator,
    get_name_of_function_wrapped_in_pydantic_validator,
    is_sqlalchemy_model,
    lenient_issubclass,
)
from cadwyn.exceptions import CadwynError, InvalidGenerationInstructionError
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import ResponseInfo
from cadwyn.structure.enums import AlterEnumSubInstruction, EnumDidntHaveMembersInstruction, EnumHadMembersInstruction
from cadwyn.structure.schemas import (
    AlterSchemaSubInstruction,
    FieldDidntExistInstruction,
    FieldDidntHaveInstruction,
    FieldExistedAsInstruction,
    FieldHadInstruction,
    SchemaHadInstruction,
    ValidatorDidntExistInstruction,
    ValidatorExistedInstruction,
    _get_model_decorators,
)
from cadwyn.structure.versions import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME, VersionBundle

from ._utils import iscoroutinefunction

if TYPE_CHECKING:
    from cadwyn.structure.versions import HeadVersion, Version


_Call = TypeVar("_Call", bound=Callable[..., Any])

_FieldName: TypeAlias = str
_T_ANY_MODEL = TypeVar("_T_ANY_MODEL", bound=Union[BaseModel, Enum])
_T_ENUM = TypeVar("_T_ENUM", bound=Enum)

_T_PYDANTIC_MODEL = TypeVar("_T_PYDANTIC_MODEL", bound=BaseModel)
PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP: dict[type[_decorators.DecoratorInfo], Callable[..., Any]] = {
    ValidatorDecoratorInfo: pydantic.validator,  # ty: ignore[deprecated]  # Preserve Pydantic v1 migrations.
    FieldValidatorDecoratorInfo: pydantic.field_validator,
    FieldSerializerDecoratorInfo: pydantic.field_serializer,
    RootValidatorDecoratorInfo: pydantic.root_validator,  # ty: ignore[deprecated]  # Preserve Pydantic v1 migrations.
    ModelValidatorDecoratorInfo: pydantic.model_validator,
    ModelSerializerDecoratorInfo: pydantic.model_serializer,
    ComputedFieldInfo: pydantic.computed_field,
}
_PYDANTIC_ALL_EXPORTED_NAMES = set(pydantic.__all__)
_DEFAULT_PYDANTIC_CLASSES = (BaseModel, RootModel)

try:
    from pydantic_settings import BaseSettings

    _DEFAULT_PYDANTIC_CLASSES = (*_DEFAULT_PYDANTIC_CLASSES, BaseSettings)
except ImportError:  # pragma: no cover
    pass


VALIDATOR_CONFIG_KEY = "__validators__"
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


@dataclasses.dataclass(**DATACLASS_SLOTS)
class PydanticFieldWrapper:
    """We DO NOT maintain field.metadata at all"""

    init_model_field: dataclasses.InitVar[FieldInfo]

    annotation: Any
    name_from_newer_version: str

    passed_field_attributes: dict[str, Any] = dataclasses.field(init=False)

    def __post_init__(self, init_model_field: FieldInfo) -> None:
        self.passed_field_attributes = _extract_passed_field_attributes(init_model_field)

    def update_attribute(self, *, name: str, value: Any) -> None:
        self.passed_field_attributes[name] = value

    def delete_attribute(self, *, name: str) -> None:
        self.passed_field_attributes.pop(name)


def _extract_passed_field_attributes(field_info: FieldInfo) -> dict[str, object]:
    return {
        k: v
        for k, v in (field_info._attributes_set | collect_known_metadata(field_info.metadata)[0]).items()
        if k in _all_field_arg_names and not (k == "frozen" and v is None)
    }


@dataclasses.dataclass(**DATACLASS_SLOTS)
class _ModelBundle:
    enums: dict[type[Enum], "_EnumWrapper[Enum]"]
    schemas: dict[type[BaseModel], "_PydanticModelWrapper[BaseModel]"]


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _RuntimeSchemaGenContext:
    version_bundle: "VersionBundle"
    current_version: "Union[Version, HeadVersion]"
    models: _ModelBundle
    latest_version: "Version" = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.latest_version = max(self.version_bundle.versions, key=lambda v: v.value)


def migrate_response_body(
    versions: "VersionBundle",
    latest_response_model: type[pydantic.BaseModel],
    *,
    latest_body: Any,
    version: Union[VersionType, date],
) -> Any:
    """Convert the data to a specific version

    Apply all version changes from latest until the passed version in reverse order
    and wrap the result in the correct version of latest_response_model
    """
    if isinstance(version, date):
        version = version.isoformat()
        version = versions._get_closest_lesser_version(version)
    if version not in versions._version_values_set:
        raise CadwynError(f"Version {version} not found in version bundle")
    response = ResponseInfo(Response(status_code=200), body=latest_body)
    migrated_response = versions._migrate_response(
        response,
        current_version=version,
        head_response_model=latest_response_model,
        head_route=None,
    )

    versioned_response_model: type[pydantic.BaseModel] = generate_versioned_models(versions)[str(version)][
        latest_response_model
    ]
    return versioned_response_model.model_validate(migrated_response.body)


def _unwrap_model(model: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
    while hasattr(model, "__cadwyn_original_model__"):
        model = cast("type[_T_ANY_MODEL]", model.__cadwyn_original_model__)
    return model


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _ValidatorWrapper:
    kwargs: dict[str, Any]
    func: Callable[..., object]
    decorator: Callable[..., Callable[..., object]]
    is_deleted: bool = False
    is_classmethod: bool = False


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _PerFieldValidatorWrapper(_ValidatorWrapper):
    fields: list[str] = dataclasses.field(default_factory=list)


def _wrap_validator(
    func: "_decorators.DecoratedType[object]",
    is_pydantic_v1_style_validator: Any,
    decorator_info: _decorators.DecoratorInfo,
):
    # This is only for pydantic v1 style validators
    func = fully_unwrap_decorator(func, is_pydantic_v1_style_validator)
    is_classmethod = False
    if inspect.ismethod(func):
        is_classmethod = True
        func = func.__func__
    kwargs = dataclasses.asdict(decorator_info)
    decorator_fields = kwargs.pop("fields", None)

    # wrapped_property is not accepted by computed_field()
    if isinstance(decorator_info, ComputedFieldInfo):
        kwargs.pop("wrapped_property", None)

    actual_decorator = PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP[type(decorator_info)]
    if is_pydantic_v1_style_validator:
        # There's an inconsistency in their interfaces so we gotta resort to this
        mode = kwargs.pop("mode", "after")
        kwargs["pre"] = mode != "after"
        if (
            isinstance(decorator_info, RootValidatorDecoratorInfo) and decorator_info.mode == "after"
        ):  # pragma: no cover # TODO
            kwargs["skip_on_failure"] = True
    if decorator_fields is not None:
        return _PerFieldValidatorWrapper(
            func=func,
            fields=list(decorator_fields),
            decorator=actual_decorator,
            kwargs=kwargs,
            is_classmethod=is_classmethod,
        )
    else:
        return _ValidatorWrapper(func=func, decorator=actual_decorator, kwargs=kwargs, is_classmethod=is_classmethod)


def _is_dunder(attr_name: str):
    return attr_name.startswith("__") and attr_name.endswith("__")


def _rebuild_model(model: type[BaseModel]) -> None:
    # In case we have a forwardref within one of the fields
    # For example, when "from __future__ import annotations" is used in the file with the schema
    if model is not BaseModel:
        # An empty namespace makes sure that our local variables cannot be used to resolve the forward refs
        model.model_rebuild(raise_errors=False, _types_namespace={})


def _get_annotations_without_evaluating(obj: Any) -> dict[str, Any]:
    """Return the annotations of a class or a function, keeping the ones that cannot be evaluated as forward refs"""
    if sys.version_info >= (3, 14):  # pragma: no cover
        from annotationlib import Format, get_annotations

        # Annotations are evaluated lazily since Python 3.14 so the names that are only imported for type checking
        # would raise a NameError
        return get_annotations(obj, format=Format.FORWARDREF)
    return obj.__annotations__


def _has_undefined_names_in_lazy_annotations(obj: Any) -> bool:
    """Whether the annotations of a class or a function refer to names that are not defined yet.

    Since Python 3.14, annotations are evaluated lazily and _get_annotations_without_evaluating keeps such names
    as forward refs, which would resolve to other objects once the names get defined. Earlier versions evaluate
    annotations when they are defined unless they are strings, which stay strings.
    """
    if sys.version_info >= (3, 14):  # pragma: no cover
        from annotationlib import Format, get_annotations

        try:
            get_annotations(obj, format=Format.VALUE)
        except NameError:
            return True
    return False


def _get_model_annotations(model: type[BaseModel], fields: Union[dict[str, FieldInfo], None] = None) -> dict[str, Any]:
    fields = model.model_fields if fields is None else fields

    def _rebuild_annotated(name: str, value: Any):
        if field_info := fields.get(name):
            if not field_info.metadata:
                return field_info.annotation

            return _AnnotatedAlias(field_info.annotation, tuple(field_info.metadata))
        return value  # pragma: no cover

    # Pydantic has already resolved the annotations of the fields so we prefer them over unresolved annotations
    return {
        name: value if not isinstance(value, str | ForwardRef) else _rebuild_annotated(name, value)
        for name, value in _get_annotations_without_evaluating(model).items()
    }


def _get_model_field_names(model: type[BaseModel], fields: Union[dict[str, FieldInfo], None] = None) -> list[str]:
    fields = model.model_fields if fields is None else fields
    return [name for name in _get_annotations_without_evaluating(model) if name in fields]


# The signature of this private function differs between pydantic versions
_REBUILD_MODEL_FIELDS_TAKES_CONFIG = "config_wrapper" in inspect.signature(rebuild_model_fields).parameters


def _get_rebuilt_model_fields(model: type[BaseModel]) -> Union[dict[str, FieldInfo], None]:
    """Return the fields that _rebuild_model would give the model without rebuilding it, if they can be resolved.

    model_rebuild first resolves the fields with the same pydantic function and namespaces. Unlike model_rebuild,
    it does not change the model, build its schema, or call its hooks.
    """
    if model.__pydantic_fields_complete__:
        return model.model_fields
    ns_resolver = NsResolver(parent_namespace=unpack_lenient_weakvaluedict(model.__pydantic_parent_namespace__) or {})
    config_kwargs = (
        {"config_wrapper": ConfigWrapper(model.model_config, check=False)} if _REBUILD_MODEL_FIELDS_TAKES_CONFIG else {}
    )
    try:
        rebuilt_fields = rebuild_model_fields(
            model, ns_resolver=ns_resolver, typevars_map=get_model_typevars_map(model), **config_kwargs
        )
    except NameError:
        # Some names in the annotations are not defined yet
        return None
    # Pydantic >= 2.13 also returns the info about the extra fields
    return rebuilt_fields[0] if isinstance(rebuilt_fields, tuple) else rebuilt_fields


def _get_head_model_references(model: type[BaseModel]) -> "_ModelReferences":
    """Return the models that the copy of a model without instructions would request from the generator.

    Unlike _wrap_pydantic_model, it does not copy anything from the model because the model may never be copied.
    """
    fields = _get_rebuilt_model_fields(model)
    if fields is None or _has_undefined_names_in_lazy_annotations(model):
        # The models that it refers to are unknown until the missing names get defined
        return _ModelReferences(frozenset(), frozenset(), is_complete=False)
    collector = _ReferencedModelCollector()
    _change_version_of_model_references(
        collector,
        model,
        {name: _extract_passed_field_attributes(fields[name]) for name in _get_model_field_names(model, fields)},
        _get_model_annotations(model, fields),
    )
    return collector.get_references()


def _change_version_of_model_references(
    generator: "Union[SchemaGenerator, _ReferencedModelCollector]",
    model: type[BaseModel],
    field_attributes: dict[str, dict[str, Any]],
    annotations: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...], dict[str, Any], PydanticGenericMetadata]:
    """Version everything in the model that can refer to other models: field attributes, bases, and annotations"""
    versioned_field_attributes = {
        name: generator.annotation_transformer.change_version_of_annotation(attributes)
        for name, attributes in field_attributes.items()
    }
    # __orig_bases__ keeps bases such as Generic[T] that __bases__ loses so that the copy stays generic
    orig_bases = tuple(
        generator[base] if isinstance(base, type) else base
        for base in model.__dict__.get("__orig_bases__", model.__bases__)
    )
    versioned_annotations = generator.annotation_transformer.change_version_of_annotation(annotations)
    generic_metadata = generator.change_version_of_generic_metadata(model)
    return versioned_field_attributes, orig_bases, versioned_annotations, generic_metadata


def _wrap_pydantic_model(model: type[_T_PYDANTIC_MODEL]) -> "_PydanticModelWrapper[_T_PYDANTIC_MODEL]":
    _rebuild_model(model)
    decorators = _get_model_decorators(model)
    validators = {}
    for decorator_wrapper in decorators:
        if decorator_wrapper.cls_var_name not in model.__dict__:
            continue

        wrapped_validator = _wrap_validator(decorator_wrapper.func, decorator_wrapper.shim, decorator_wrapper.info)
        validators[decorator_wrapper.cls_var_name] = wrapped_validator

    annotations = _get_model_annotations(model)
    fields = {
        field_name: PydanticFieldWrapper(
            model.model_fields[field_name],
            annotations[field_name],
            field_name,
        )
        for field_name in _get_model_field_names(model)
    }

    main_attributes = fields | validators
    other_attributes = {
        attr_name: attr_val
        for attr_name, attr_val in model.__dict__.items()
        if attr_name not in main_attributes
        and not (_is_dunder(attr_name) or attr_name in {"_abc_impl", "model_fields", "model_computed_fields"})
    }

    other_attributes |= {
        "model_config": model.model_config,
        "__module__": model.__module__,
        "__qualname__": model.__qualname__,
    }
    return _PydanticModelWrapper(
        model,
        name=model.__name__,
        doc=model.__doc__,
        fields=fields,
        other_attributes=other_attributes,
        validators=validators,
        annotations=annotations,
    )


@final
@dataclasses.dataclass(**DATACLASS_SLOTS)
class _PydanticModelWrapper(Generic[_T_PYDANTIC_MODEL]):
    cls: type[_T_PYDANTIC_MODEL] = dataclasses.field(repr=False)
    name: str
    doc: Union[str, None] = dataclasses.field(repr=False)
    fields: Annotated[
        dict["_FieldName", PydanticFieldWrapper],
        Doc(
            "Fields that belong to this model, not to its parents. "
            "I.e. The ones that were either defined or overridden "
        ),
    ] = dataclasses.field(repr=False)
    validators: dict[str, Union[_PerFieldValidatorWrapper, _ValidatorWrapper]] = dataclasses.field(repr=False)
    other_attributes: dict[str, Any] = dataclasses.field(repr=False)
    annotations: dict[str, Any] = dataclasses.field(repr=False)
    _parents: Union[list["_PydanticModelWrapper[BaseModel]"], None] = dataclasses.field(
        init=False, default=None, repr=False
    )

    def __post_init__(self) -> None:
        # This isn't actually supposed to run, it's just a precaution
        while hasattr(self.cls, "__cadwyn_original_model__"):  # pragma: no cover
            self.cls = cast("type[_T_PYDANTIC_MODEL]", self.cls.__cadwyn_original_model__)

        for k, annotation in self.annotations.items():
            if get_origin(annotation) == Annotated:
                sub_annotations = get_args(annotation)
                # Annotated cannot be copied and is cached based on "==" and "hash", while annotated_types.Interval are
                # frozen and so are consistently hashed
                self.annotations[k] = _AnnotatedAlias(
                    copy.deepcopy(sub_annotations[0]), tuple(copy.deepcopy(sub_ann) for sub_ann in sub_annotations[1:])
                )

    def __deepcopy__(self, memo: dict[int, Any]) -> "_PydanticModelWrapper[_T_PYDANTIC_MODEL]":
        result = _PydanticModelWrapper(
            self.cls,
            name=self.name,
            doc=self.doc,
            fields=copy.deepcopy(self.fields),
            validators=copy.deepcopy(self.validators),
            other_attributes=copy.deepcopy(self.other_attributes),
            annotations=copy.deepcopy(self.annotations),
        )
        memo[id(self)] = result
        return result

    @override
    def __hash__(self) -> int:
        return hash(id(self))

    def _get_parents(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> "list[_PydanticModelWrapper[BaseModel]]":
        if self._parents is not None:
            return self._parents
        parents: list[_PydanticModelWrapper[BaseModel]] = []
        for base in self.cls.mro()[1:]:
            if not issubclass(base, BaseModel):
                continue
            if base in schemas:
                parents.append(schemas[base])
            else:
                parents.append(_wrap_pydantic_model(base))
        self._parents = parents
        return parents

    def _get_defined_fields_through_mro(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> dict[str, PydanticFieldWrapper]:
        fields = {}

        for parent in reversed(self._get_parents(schemas)):
            fields |= parent.fields

        return fields | self.fields

    def _get_defined_annotations_through_mro(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> dict[str, Any]:
        annotations = {}

        for parent in reversed(self._get_parents(schemas)):
            annotations |= parent.annotations

        return annotations | self.annotations

    def get_references(self) -> "_ModelReferences":
        """Return the models that generate_model_copy requests from the generator"""
        collector = _ReferencedModelCollector()
        self._change_version_of_references(collector)
        return collector.get_references()

    def _change_version_of_references(
        self, generator: "Union[SchemaGenerator, _ReferencedModelCollector]"
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...], dict[str, Any], PydanticGenericMetadata]:
        return _change_version_of_model_references(
            generator,
            self.cls,
            {name: field.passed_field_attributes for name, field in self.fields.items()},
            self.annotations,
        )

    def generate_model_copy(self, generator: "SchemaGenerator") -> type[_T_PYDANTIC_MODEL]:
        per_field_validators = {}
        root_validators = {}
        for name, validator in self.validators.items():
            if validator.is_deleted:
                continue
            func = classmethod(validator.func) if validator.is_classmethod else validator.func
            if isinstance(validator, _PerFieldValidatorWrapper):
                per_field_validators[name] = validator.decorator(*validator.fields, **validator.kwargs)(func)
            else:
                root_validators[name] = validator.decorator(**validator.kwargs)(func)
        field_attributes, orig_bases, annotations, generic_metadata = self._change_version_of_references(generator)
        fields = {name: pydantic.Field(**attributes) for name, attributes in field_attributes.items()}
        orig_bases_attribute = {"__orig_bases__": orig_bases} if "__orig_bases__" in self.cls.__dict__ else {}

        model_copy = type(self.cls)(
            self.name,
            types.resolve_bases(orig_bases),
            self.other_attributes
            | orig_bases_attribute
            | per_field_validators
            | root_validators
            | fields
            | {
                "__annotations__": annotations,
                "__doc__": self.doc,
                "__qualname__": self.cls.__qualname__.removesuffix(self.cls.__name__) + self.name,
            },
            __pydantic_generic_metadata__=generic_metadata,
        )

        model_copy.__cadwyn_original_model__ = self.cls  # ty: ignore[unresolved-attribute]
        return cast("type[_T_PYDANTIC_MODEL]", model_copy)


def _get_annotated_function(call: Any) -> Any:
    """Return the function whose annotations _CallableWrapper wraps, if any"""
    call = _AnnotationTransformer._unwrap_callable(call)
    if not is_regular_function(call):
        call = call.__call__
    return call if is_regular_function(call) else None


def _get_raw_callable_annotations(call: Any) -> dict[str, Any]:
    """Return the annotations of the function that _CallableWrapper wraps"""
    function = _get_annotated_function(call)
    return {} if function is None else _get_annotations_without_evaluating(function)


def _get_signature_parameters(call: Any) -> types.MappingProxyType[str, inspect.Parameter]:
    if sys.version_info >= (3, 14):  # pragma: no cover
        from annotationlib import Format

        # The annotations of the signature are evaluated lazily since Python 3.14 so the names that are only
        # imported for type checking would raise a NameError. We only need the names and the defaults anyway.
        return inspect.signature(call, annotation_format=Format.FORWARDREF).parameters
    return inspect.signature(call).parameters


def _get_type_alias_value(alias: Any) -> Any:
    # The values of typing.TypeAliasType (but not of typing_extensions.TypeAliasType) are evaluated lazily so they
    # can contain names that are only imported for type checking
    if sys.version_info >= (3, 14) and hasattr(alias, "evaluate_value"):  # pragma: no cover
        from annotationlib import Format, call_evaluate_function

        return call_evaluate_function(alias.evaluate_value, Format.FORWARDREF)
    return alias.__value__


def is_regular_function(call: object) -> TypeGuard[Union[types.FunctionType, types.MethodType]]:
    return isinstance(call, types.FunctionType | types.MethodType)


def _function_globals(call: Union[types.FunctionType, types.MethodType]) -> dict[str, Any]:
    return call.__globals__  # ty: ignore[unresolved-attribute]  # MethodType forwards function attributes at runtime.


class _CallableWrapper:
    __annotations__: dict[str, Any]
    __defaults__: Union[tuple[Any, ...], None]
    __signature__: inspect.Signature

    # __eq__ and __hash__ are needed to make sure that dependency overrides work correctly.
    # They are based on putting dependencies (functions) as keys for the dictionary so if we want to be able to
    # override the wrapper, we need to make sure that it is equivalent to the original in __hash__ and __eq__

    def __init__(self, original_callable: Callable[..., Any]) -> None:
        super().__init__()
        self._original_callable = original_callable
        if not is_regular_function(original_callable):
            original_callable = original_callable.__call__

        functools.update_wrapper(self, original_callable)

    @property
    def __globals__(self) -> dict[str, Any]:  # pragma: no cover
        # FastAPI uses __globals__ to resolve forward references in type hints
        # It's supposed to be an attribute on the function but we use it as property to prevent python
        # from trying to pickle globals when we deepcopy this wrapper
        if is_regular_function(self._original_callable):
            return _function_globals(self._original_callable)
        dunder_call = self._original_callable.__call__
        if is_regular_function(dunder_call):
            return _function_globals(dunder_call)
        return {}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._original_callable(*args, **kwargs)

    @override
    def __hash__(self) -> int:
        return hash(self._original_callable)

    @override
    def __eq__(self, value: object) -> bool:
        return self._original_callable == value


class _AsyncCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._original_callable(*args, **kwargs)


class _GeneratorCallableWrapper(_CallableWrapper):
    @override
    def __call__(self, *args: Any, **kwargs: Any) -> typing.Iterator[Any]:  # pragma: no cover
        yield from self._original_callable(*args, **kwargs)


class _AsyncGeneratorCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> typing.AsyncIterator[Any]:  # pragma: no cover
        async for value in self._original_callable(*args, **kwargs):
            yield value


@final
class _AnnotationTransformer:
    def __init__(
        self, generator: "Union[SchemaGenerator, _ReferencedModelCollector]", *, record_only: bool = False
    ) -> None:
        self.generator = generator
        # Only request the models from the generator without rebuilding the annotations. This way, we can find
        # the models that an annotation refers to without failing on annotations that we cannot rebuild
        self.record_only = record_only
        # The type aliases whose values we are recording, to detect the ones that refer to themselves
        self._type_aliases_being_recorded: list[Any] = []
        self.change_versions_of_a_non_container_annotation = self._change_version_of_a_non_container_annotation
        # This cache cannot be used until something is done regarding https://github.com/fastapi/fastapi/pull/14320
        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/FastAPI do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        # self.change_versions_of_a_non_container_annotation = functools.cache(
        #     self._change_version_of_a_non_container_annotation,
        # )

    def change_version_of_annotation(self, annotation: Any) -> Any:
        """Recursively go through all annotations and change them to annotations corresponding to the version passed.

        So if we had a annotation "UserResponse" from "head" version, and we passed version of "2022-11-16", it would
        replace "UserResponse" with the the same class but from the "2022-11-16" version.

        """
        if isinstance(annotation, dict):
            items = [
                (self.change_version_of_annotation(key), self.change_version_of_annotation(value))
                for key, value in annotation.items()
            ]
            return annotation if self.record_only else dict(items)

        elif isinstance(annotation, list | tuple):
            values = [self.change_version_of_annotation(v) for v in annotation]
            return annotation if self.record_only else type(annotation)(values)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation)

    def migrate_router_to_version(self, router: fastapi.routing.APIRouter):
        for route in router.routes:
            if not isinstance(route, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(route)

    def migrate_route_to_version(self, route: fastapi.routing.APIRoute, *, ignore_response_model: bool = False):
        if route.response_model is not None and not ignore_response_model:
            route.response_model = self.change_version_of_annotation(route.response_model)
            route.response_field = fastapi.utils.create_model_field(
                name="Response_" + route.unique_id,
                type_=route.response_model,
                mode="serialization",
            )
        route.dependencies = self.change_version_of_annotation(route.dependencies)
        route.endpoint = self.change_version_of_annotation(route.endpoint)
        for callback in route.callbacks or []:
            if not isinstance(callback, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(callback, ignore_response_model=ignore_response_model)
        self._remake_endpoint_dependencies(route)

    def _change_version_of_a_non_container_annotation(self, annotation: Any) -> Any:
        from typing_inspection.typing_objects import is_any, is_newtype, is_typealiastype

        if isinstance(annotation, GenericAliasUnionArgs):
            return self._change_version_of_generic_alias(annotation)
        elif is_typealiastype(annotation):
            if (
                annotation.__module__ is not None and (annotation.__module__.startswith("pydantic."))
            ) or annotation.__name__ in _PYDANTIC_ALL_EXPORTED_NAMES:
                return annotation
            if self.record_only:
                self._record_type_alias(annotation)
                return annotation
            value = self.change_version_of_annotation(_get_type_alias_value(annotation))
            type_params = self.change_version_of_annotation(annotation.__type_params__)
            return type(annotation)(name=annotation.__name__, value=value, type_params=type_params)
        elif isinstance(annotation, fastapi.params.Depends):
            dependency = self.change_version_of_annotation(annotation.dependency)
            if self.record_only:
                return annotation
            if isinstance(annotation, fastapi.params.Security):
                return fastapi.params.Security(dependency, scopes=annotation.scopes, use_cache=annotation.use_cache)
            return fastapi.params.Depends(dependency, use_cache=annotation.use_cache)
        elif isinstance(annotation, UnionType):  # pragma: no cover
            args = tuple(self.change_version_of_annotation(a) for a in get_args(annotation))
            return annotation if self.record_only else typing.Union[args]
        elif is_any(annotation) or is_newtype(annotation):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation)
        elif callable(annotation):
            return self._change_version_of_callable(annotation)
        else:
            return annotation

    def _change_version_of_callable(self, annotation: Callable[..., Any]) -> Any:
        # Special forms such as typing.Self are callable but they are not functions
        if (
            type(annotation).__module__ in {"typing", "typing_extensions"}
            or type(annotation).__module__.startswith(("fastapi.", "pydantic.", "pydantic_core.", "starlette."))
            or isinstance(annotation, fastapi.security.base.SecurityBase)
        ):
            return annotation

        try:
            parameters = _get_signature_parameters(annotation)
        except (ValueError, TypeError):
            # Callables without an inspectable signature such as `functools.partial(dict, count=1)`
            # have no annotations or defaults to version
            return annotation

        def modifier(annotation: Any):
            return self.change_version_of_annotation(annotation)

        if self.record_only:
            callable_annotations, defaults = self._get_callable_annotations_and_defaults(
                annotation, _get_raw_callable_annotations(annotation), parameters
            )
            function = _get_annotated_function(annotation)
            # The string annotations that cannot be evaluated yet stay strings
            if any(type(value) is str for value in callable_annotations.values()) or (
                function is not None and _has_undefined_names_in_lazy_annotations(function)
            ):
                self._record_unknown_references()
            modifier(callable_annotations)
            modifier(defaults)
            return annotation
        return self._modify_callable_annotations(
            annotation,
            modifier,
            modifier,
            annotation_modifying_wrapper_factory=self._copy_function_through_class_based_wrapper,
        )

    def _record_type_alias(self, alias: Any) -> None:
        if alias in self._type_aliases_being_recorded:
            # Copying a type alias that refers to itself (e.g. `type Json = dict[str, Json] | int`) would recurse
            # forever, as it does on main
            self._record_unknown_references()
            return
        try:
            # The values of native type aliases are evaluated lazily since Python 3.12
            alias.__value__  # noqa: B018
        except NameError:
            self._record_unknown_references()
            return
        self._type_aliases_being_recorded.append(alias)
        try:
            self.change_version_of_annotation(_get_type_alias_value(alias))
            self.change_version_of_annotation(alias.__type_params__)
        finally:
            self._type_aliases_being_recorded.pop()

    def _record_unknown_references(self) -> None:
        """Record that we cannot know every model that copying the annotation would request.

        E.g. because the annotation refers to names that are not defined yet, which could refer to any model
        once they get defined, or because copying it would never finish.
        """
        cast("_ReferencedModelCollector", self.generator).is_complete = False

    def _change_version_of_generic_alias(self, annotation: Any) -> Any:
        from typing_inspection.typing_objects import is_unpack

        origin = get_origin(annotation)
        args = get_args(annotation)
        new_args = tuple(self.change_version_of_annotation(arg) for arg in args)
        if self.record_only:
            return annotation
        # A bare `typing.Callable` has nothing to version and cannot be rebuilt as `Callable[()]`
        if origin is collections.abc.Callable and not args:
            return annotation
        # Classvar and Unpack do not support generic tuple arguments
        if origin is ClassVar or is_unpack(origin):
            return origin[new_args[0]]
        return origin[new_args]

    def _change_version_of_type(self, annotation: type) -> type:
        if lenient_issubclass(annotation, (BaseModel, Enum)):
            return self.generator[annotation]
        else:
            return annotation

    @classmethod
    def _remake_endpoint_dependencies(cls, route: fastapi.routing.APIRoute) -> None:
        # Unlike get_dependant, APIRoute is the public API of FastAPI and it's (almost) guaranteed to be stable.

        route_copy = fastapi.routing.APIRoute(
            route.path, route.endpoint, dependencies=route.dependencies, response_model=route.response_model
        )
        route.dependant = route_copy.dependant
        route.body_field = route_copy.body_field
        _add_request_and_response_params(route)

    @classmethod
    def _modify_callable_annotations(  # pragma: no branch # because of lambdas
        cls,
        call: _Call,
        modify_annotations: Callable[[dict[str, Any]], dict[str, Any]] = lambda a: a,
        modify_defaults: Callable[[tuple[Any, ...]], tuple[Any, ...]] = lambda a: a,
        *,
        annotation_modifying_wrapper_factory: Callable[[_Call], _CallableWrapper],
    ) -> _Call:
        annotation_modifying_wrapper = annotation_modifying_wrapper_factory(call)
        old_params = _get_signature_parameters(call)
        callable_annotations, defaults = cls._get_callable_annotations_and_defaults(
            call, _get_raw_callable_annotations(call), old_params
        )
        annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
        annotation_modifying_wrapper.__defaults__ = modify_defaults(defaults)
        annotation_modifying_wrapper.__signature__ = cls._generate_signature(
            annotation_modifying_wrapper,
            old_params,
        )

        return cast("_Call", annotation_modifying_wrapper)

    @classmethod
    def _get_callable_annotations_and_defaults(
        cls,
        call: Any,
        callable_annotations: dict[str, Any],
        parameters: types.MappingProxyType[str, inspect.Parameter],
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        original_call = cls._unwrap_callable(call)
        # For callable class instances, __globals__ is on the __call__ method, not on the instance itself
        if is_regular_function(original_call):
            call_globals = _function_globals(original_call)
        else:
            dunder_call = inspect.getattr_static(original_call, "__call__", None)
            call_globals = _function_globals(dunder_call) if is_regular_function(dunder_call) else {}
        callable_annotations = {
            k: v if type(v) is not str else _try_eval_type(v, call_globals) for k, v in callable_annotations.items()
        }
        defaults = tuple(p.default for p in parameters.values() if p.default is not inspect.Signature.empty)
        return callable_annotations, defaults

    @staticmethod
    def _generate_signature(
        new_callable: _CallableWrapper,
        old_params: types.MappingProxyType[str, inspect.Parameter],
    ) -> inspect.Signature:
        parameters = []
        default_counter = 0
        for param in old_params.values():
            if param.default is not inspect.Signature.empty:
                assert new_callable.__defaults__ is not None, (  # noqa: S101
                    "Defaults cannot be None here. If it is, you have found a bug in Cadwyn. "
                    "Please, report it in our issue tracker."
                )
                default = new_callable.__defaults__[default_counter]
                default_counter += 1
            else:
                default = inspect.Signature.empty
            parameters.append(
                inspect.Parameter(
                    param.name,
                    param.kind,
                    default=default,
                    annotation=new_callable.__annotations__.get(
                        param.name,
                        inspect.Signature.empty,
                    ),
                ),
            )
        return inspect.Signature(
            parameters=parameters,
            return_annotation=new_callable.__annotations__.get(
                "return",
                inspect.Signature.empty,
            ),
        )

    @classmethod
    def _copy_function_through_class_based_wrapper(cls, call: _Call) -> _CallableWrapper:
        """Separate from copy_endpoint because endpoints MUST be functions in FastAPI, they cannot be cls instances"""
        call = cls._unwrap_callable(call)
        if not is_regular_function(call):
            # This means that the callable is actually an instance of a regular class
            actual_call = call.__call__
        else:
            actual_call = call
        if is_async_gen_callable(actual_call):
            return _AsyncGeneratorCallableWrapper(call)
        elif is_coroutine_callable(actual_call):
            return _AsyncCallableWrapper(call)
        elif is_gen_callable(actual_call):
            return _GeneratorCallableWrapper(call)
        else:
            return _CallableWrapper(call)

    @staticmethod
    def _unwrap_callable(call: Any) -> Any:
        while hasattr(call, "_original_callable"):
            call = call._original_callable

        return call


def is_gen_callable(call: Callable[..., object]) -> bool:
    # Copied from fastapi.dependencies.models
    if inspect.isgeneratorfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isgeneratorfunction(dunder_call)


def is_async_gen_callable(call: Callable[..., object]) -> bool:
    # Copied from fastapi.dependencies.models
    if inspect.isasyncgenfunction(call):
        return True
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return inspect.isasyncgenfunction(dunder_call)


def is_coroutine_callable(call: Callable[..., object]) -> bool:  # pragma: no cover
    # Copied from fastapi.dependencies.models
    # ty reports the deprecation only on Python 3.11/3.12. Repeating unused-ignore-comment
    # handles the other targets: https://github.com/astral-sh/ty/issues/3393.
    if inspect.isroutine(call):
        return iscoroutinefunction(call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]  # Match FastAPI.
    if inspect.isclass(call):
        return False
    dunder_call = getattr(call, "__call__", None)  # noqa: B004
    return iscoroutinefunction(dunder_call)  # ty: ignore[deprecated, unused-ignore-comment, unused-ignore-comment]  # Match FastAPI.


def _add_request_and_response_params(route: APIRoute):
    if not route.dependant.request_param_name:
        route.dependant.request_param_name = _CADWYN_REQUEST_PARAM_NAME
    if not route.dependant.response_param_name:
        route.dependant.response_param_name = _CADWYN_RESPONSE_PARAM_NAME


def _is_versionable_model(model: Any) -> bool:
    return (
        isinstance(model, type)
        and lenient_issubclass(model, (BaseModel, Enum))
        and model not in _DEFAULT_PYDANTIC_CLASSES
        and not is_sqlalchemy_model(model)
    )


@final
class _ReferencedModelCollector:
    """A generator that records the models requested from it instead of generating their copies"""

    __slots__ = "annotation_transformer", "arg_models", "is_complete", "models"

    def __init__(self) -> None:
        self.annotation_transformer = _AnnotationTransformer(self, record_only=True)
        self.models: set[type[Union[BaseModel, Enum]]] = set()
        self.arg_models: set[type[Union[BaseModel, Enum]]] = set()
        # Whether the annotations that we looked at only refer to names that are already defined
        self.is_complete = True

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(model):
            self.models.add(_unwrap_model(model))
        return model

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        generic_metadata = model.__pydantic_generic_metadata__
        arg_collector = _ReferencedModelCollector()
        args = _eval_generic_args(model)
        arg_collector.annotation_transformer.change_version_of_annotation(args)
        self.arg_models |= arg_collector.models
        # The quoted args that cannot be evaluated yet stay forward refs
        if not arg_collector.is_complete or any(_contains_forward_refs(arg) for arg in args):
            self.is_complete = False
        if generic_metadata["origin"] is not None:
            self[generic_metadata["origin"]]
        return generic_metadata

    def get_references(self) -> "_ModelReferences":
        return _ModelReferences(frozenset(self.models), frozenset(self.arg_models), self.is_complete)


class _ModelReferences(NamedTuple):
    # The models that are always versioned along with the model: in its fields, annotations, and bases
    models: frozenset[type[Union[BaseModel, Enum]]]
    # The models in the type args of a parametrized generic (e.g. Item in Page[Item])
    arg_models: frozenset[type[Union[BaseModel, Enum]]]
    # Whether we know all of the models that copying the model would request. We do not when some names in its
    # annotations (fields, class variables, quoted generic args, annotations of default factories, values of type
    # aliases, etc.) are not defined yet: once they get defined, copying the model can request any model. We do not
    # either when copying it would recurse forever through a type alias that refers to itself. So we never version
    # the generic args that lead to such a model, even after the names get defined, which keeps their head args
    # exactly as on main.
    is_complete: bool = True


class _GenerationAnalysis(NamedTuple):
    # Whether generating the copy of the model will not recurse forever
    terminates: bool
    # Whether the model or the models that it refers to have schema or enum instructions
    refers_to_changed_models: bool


@final
class SchemaGenerator:
    __slots__ = (
        "_changed_models",
        "_generation_analyses",
        "_lock",
        "_model_references",
        "_versions_of_generic_args",
        "annotation_transformer",
        "concrete_models",
        "model_bundle",
    )

    def __init__(
        self,
        model_bundle: _ModelBundle,
        changed_models: "frozenset[type[Union[BaseModel, Enum]]]" = frozenset(),
    ) -> None:
        self.annotation_transformer = _AnnotationTransformer(self)
        self.model_bundle = model_bundle
        self._changed_models = changed_models
        self._model_references: dict[type[Union[BaseModel, Enum]], _ModelReferences] = {}
        self._generation_analyses: dict[type[Union[BaseModel, Enum]], _GenerationAnalysis] = {}
        self._versions_of_generic_args: dict[type[BaseModel], bool] = {}
        # Models are generated lazily (e.g. in migrate_response_body) so several threads can generate them at once
        self._lock = threading.RLock()
        self.concrete_models: dict[type[Union[BaseModel, Enum]], type[Union[BaseModel, Enum]]] = {}
        # Models are generated through __getitem__ so that the copies created along the way are reused
        # instead of being created again
        for model in [*self.model_bundle.schemas, *self.model_bundle.enums]:
            self[model]

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if not _is_versionable_model(model):
            return model
        model = _unwrap_model(model)

        with self._lock:
            if model not in self.concrete_models:
                wrapper = self._get_wrapper_for_model(model)
                self.concrete_models[model] = wrapper.generate_model_copy(self)
            return cast("type[_T_ANY_MODEL]", self.concrete_models[model])

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        generic_metadata = model.__pydantic_generic_metadata__
        if not self._should_version_generic_args(model, generation_stack=[]):
            return generic_metadata
        # Pydantic resolves the fields of a parametrized generic (such as Page[Item]) through its origin and args
        # so they must point to this version's classes too
        origin = generic_metadata["origin"]
        return {
            "origin": self[origin] if origin is not None else None,
            "args": self.annotation_transformer.change_version_of_annotation(_eval_generic_args(model)),
            "parameters": generic_metadata["parameters"],
        }

    def _should_version_generic_args(
        self, generic: type[BaseModel], generation_stack: list[type[Union[BaseModel, Enum]]]
    ) -> bool:
        """Whether the copy of the generic should use the copies of its type args instead of the head ones.

        We only version the args when they refer to models with schema or enum instructions so that all other
        generics stay exactly as they are. We also keep the head args when their copies could not be generated:
        when the args refer back to the generic (e.g. `children: Page["Category"]` within `Category`) or when
        generating their copies would recurse forever (e.g. `children: "list[Tree]"` within `Tree`).
        The decision only depends on the references between the models, not on the order of generation.
        """
        if generic not in self._versions_of_generic_args:
            references = self._get_references(generic)
            arg_models = references.arg_models
            # Versioning the args of a generic whose references are incomplete could request any model
            if not arg_models or not references.is_complete or self._is_referenced_through_args(generic):
                should_version = False
            else:
                analyses = [self._analyze_generation(model, generation_stack) for model in arg_models]
                should_version = all(analysis.terminates for analysis in analyses) and any(
                    analysis.refers_to_changed_models for analysis in analyses
                )
            self._versions_of_generic_args[generic] = should_version
        return self._versions_of_generic_args[generic]

    def _analyze_generation(
        self, model: type[Union[BaseModel, Enum]], generation_stack: list[type[Union[BaseModel, Enum]]]
    ) -> _GenerationAnalysis:
        if model in self._generation_analyses:
            return self._generation_analyses[model]
        if model in generation_stack:
            # The models that generating this model requests lead back to it so its generation would never finish
            return _GenerationAnalysis(terminates=False, refers_to_changed_models=False)
        generation_stack.append(model)
        references = self._get_references(model)
        requested_models = set(references.models)
        if references.arg_models and self._should_version_generic_args(
            cast("type[BaseModel]", model), generation_stack
        ):
            requested_models |= references.arg_models
        analyses = [self._analyze_generation(requested_model, generation_stack) for requested_model in requested_models]
        generation_stack.pop()
        analysis = _GenerationAnalysis(
            terminates=references.is_complete and all(analysis.terminates for analysis in analyses),
            refers_to_changed_models=model in self._changed_models
            or any(analysis.refers_to_changed_models for analysis in analyses),
        )
        self._generation_analyses[model] = analysis
        return analysis

    def _is_referenced_through_args(self, generic: type[BaseModel]) -> bool:
        models_to_visit = list(self._get_references(generic).arg_models)
        visited_models = set()
        while models_to_visit:
            referenced_model = models_to_visit.pop()
            if _is_same_parametrization(referenced_model, generic):
                return True
            if referenced_model not in visited_models:
                visited_models.add(referenced_model)
                references = self._get_references(referenced_model)
                models_to_visit.extend(references.models | references.arg_models)
        return False

    def _get_references(self, model: type[Union[BaseModel, Enum]]) -> _ModelReferences:
        """Return the models that copying the model requests from this generator (reference discovery).

        INVARIANT: discovery must find a superset of the models that copying the model actually requests.
        The decision to version generic args (the cycle rule) relies on these references to rule out the cycles
        that would make copying recurse forever, so any model that copying requests and discovery misses can lead
        to infinite recursion. This is why discovery runs the same function as copying
        (_change_version_of_model_references) and why tests/conftest.py checks the invariant for every model
        copied in the test suite. If discovery cannot know all of the references because some names are not defined
        yet (see _ModelReferences.is_complete), it must say so instead of returning a subset.
        Discovery must not change the model either because the model may never be copied.
        The references and the decisions based on them are cached for the lifetime of the generator, just like
        the copies, so a generic keeps the args that it got when the generator first needed it.
        """
        if model not in self._model_references:
            if lenient_issubclass(model, Enum):
                references = _ModelReferences(frozenset(), frozenset())
            elif model in self.model_bundle.schemas:
                references = self.model_bundle.schemas[model].get_references()
            else:
                # We do not wrap the model because wrapping copies parts of it and we may never need its copy
                references = _get_head_model_references(cast("type[BaseModel]", model))
            self._model_references[model] = references
        return self._model_references[model]

    @overload
    def _get_wrapper_for_model(self, model: type[BaseModel]) -> "_PydanticModelWrapper[BaseModel]": ...
    @overload
    def _get_wrapper_for_model(self, model: type[Enum]) -> "_EnumWrapper[Enum]": ...
    @overload
    def _get_wrapper_for_model(
        self, model: type[Union[BaseModel, Enum]]
    ) -> "Union[_PydanticModelWrapper[BaseModel], _EnumWrapper[Enum]]": ...

    def _get_wrapper_for_model(
        self, model: type[Union[BaseModel, Enum]]
    ) -> "Union[_PydanticModelWrapper[BaseModel], _EnumWrapper[Enum]]":
        model = _unwrap_model(model)

        if issubclass(model, BaseModel):
            if model in self.model_bundle.schemas:
                return self.model_bundle.schemas[model]
            # TODO: My god, what if one of its fields is in our concrete schemas and we don't use it? :O
            # TODO: Add an argument with our concrete schemas for _wrap_pydantic_model
            wrapper = _wrap_pydantic_model(model)
            self.model_bundle.schemas[model] = wrapper
        elif issubclass(model, Enum):
            if model in self.model_bundle.enums:
                return self.model_bundle.enums[model]
            wrapper = _EnumWrapper(model)
            self.model_bundle.enums[model] = wrapper
        else:
            assert_never(model)
        return wrapper


@cache
def generate_versioned_models(versions: "VersionBundle") -> "dict[str, SchemaGenerator]":
    models = _create_model_bundle(versions)
    changed_models = frozenset(
        instruction.schema
        for version in versions._all_versions
        for version_change in version.changes
        for instruction in version_change.alter_schema_instructions
    ) | frozenset(
        instruction.enum
        for version in versions._all_versions
        for version_change in version.changes
        for instruction in version_change.alter_enum_instructions
    )

    version_to_context_map = {}
    context = _RuntimeSchemaGenContext(current_version=versions.head_version, models=models, version_bundle=versions)
    _migrate_classes(context)

    for version in versions.versions:
        context = _RuntimeSchemaGenContext(current_version=version, models=models, version_bundle=versions)
        version_to_context_map[str(version.value)] = SchemaGenerator(copy.deepcopy(models), changed_models)
        # note that the last migration will not contain any version changes so we don't need to save the results
        _migrate_classes(context)

    return version_to_context_map


def _create_model_bundle(versions: "VersionBundle"):
    return _ModelBundle(
        enums={enum: _EnumWrapper(enum) for enum in versions.versioned_enums.values()},
        schemas={schema: _wrap_pydantic_model(schema) for schema in versions.versioned_schemas.values()},
    )


def _migrate_classes(context: _RuntimeSchemaGenContext) -> None:
    for version_change in context.current_version.changes:
        _apply_alter_schema_instructions(
            context.models.schemas,
            version_change.alter_schema_instructions,
            version_change.__name__,
        )
        _apply_alter_enum_instructions(
            context.models.enums,
            version_change.alter_enum_instructions,
            version_change.__name__,
        )


def _apply_alter_schema_instructions(
    modified_schemas: dict[type[BaseModel], _PydanticModelWrapper[BaseModel]],
    alter_schema_instructions: Sequence[Union[AlterSchemaSubInstruction, SchemaHadInstruction]],
    version_change_name: str,
) -> None:
    for alter_schema_instruction in alter_schema_instructions:
        schema_info = modified_schemas[alter_schema_instruction.schema]
        if isinstance(alter_schema_instruction, FieldExistedAsInstruction):
            _add_field_to_model(schema_info, modified_schemas, alter_schema_instruction, version_change_name)
        elif isinstance(alter_schema_instruction, FieldHadInstruction | FieldDidntHaveInstruction):
            _change_field_in_model(
                schema_info,
                modified_schemas,
                alter_schema_instruction,
                version_change_name,
            )
        elif isinstance(alter_schema_instruction, FieldDidntExistInstruction):
            _delete_field_from_model(schema_info, alter_schema_instruction.name, version_change_name)
        elif isinstance(alter_schema_instruction, ValidatorExistedInstruction):
            validator_name = get_name_of_function_wrapped_in_pydantic_validator(alter_schema_instruction.validator)
            raw_validator = cast(
                "pydantic._internal._decorators.PydanticDescriptorProxy[object]",
                alter_schema_instruction.validator,
            )
            schema_info.validators[validator_name] = _wrap_validator(
                raw_validator.wrapped,
                is_pydantic_v1_style_validator=raw_validator.shim,
                decorator_info=raw_validator.decorator_info,
            )
        elif isinstance(alter_schema_instruction, ValidatorDidntExistInstruction):
            if alter_schema_instruction.name not in schema_info.validators:
                raise InvalidGenerationInstructionError(
                    f'You tried to delete a validator "{alter_schema_instruction.name}" from "{schema_info.name}" '
                    f'in "{version_change_name}" but it doesn\'t have such a validator.',
                )
            if schema_info.validators[alter_schema_instruction.name].is_deleted:
                raise InvalidGenerationInstructionError(
                    f'You tried to delete a validator "{alter_schema_instruction.name}" from "{schema_info.name}" '
                    f'in "{version_change_name}" but it is already deleted.',
                )
            schema_info.validators[alter_schema_instruction.name].is_deleted = True
        elif isinstance(alter_schema_instruction, SchemaHadInstruction):
            _change_model(schema_info, alter_schema_instruction, version_change_name)
        else:
            assert_never(alter_schema_instruction)


def _apply_alter_enum_instructions(
    enums: "dict[type[Enum], _EnumWrapper[Enum]]",
    alter_enum_instructions: Sequence[AlterEnumSubInstruction],
    version_change_name: str,
):
    for alter_enum_instruction in alter_enum_instructions:
        enum = enums[alter_enum_instruction.enum]
        if isinstance(alter_enum_instruction, EnumDidntHaveMembersInstruction):
            for member in alter_enum_instruction.members:
                if member not in enum.members:
                    raise InvalidGenerationInstructionError(
                        f'You tried to delete a member "{member}" from "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but it doesn\'t have such a member.',
                    )
                enum.members.pop(member)
        elif isinstance(alter_enum_instruction, EnumHadMembersInstruction):
            for member, member_value in alter_enum_instruction.members.items():
                if member in enum.members and enum.members[member] == member_value:
                    raise InvalidGenerationInstructionError(
                        f'You tried to add a member "{member}" to "{enum.cls.__name__}" '
                        f'in "{version_change_name}" but there is already a member with that name and value.',
                    )
                enum.members[member] = member_value
        else:
            assert_never(alter_enum_instruction)


def _change_model(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: SchemaHadInstruction,
    version_change_name: str,
):
    if alter_schema_instruction.name == model.name:
        raise InvalidGenerationInstructionError(
            f'You tried to change the name of "{model.name}" in "{version_change_name}" '
            "but it already has the name you tried to assign.",
        )

    model.name = alter_schema_instruction.name


def _add_field_to_model(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    alter_schema_instruction: FieldExistedAsInstruction,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to add a field "{alter_schema_instruction.name}" to "{model.name}" '
            f'in "{version_change_name}" but there is already a field with that name.',
        )

    # Special handling for ClassVar fields
    if get_origin(alter_schema_instruction.field.annotation) is ClassVar:
        # ClassVar fields should not be in model.fields, only in annotations and other_attributes
        model.annotations[alter_schema_instruction.name] = alter_schema_instruction.field.annotation
        # Set the actual ClassVar value in other_attributes
        if alter_schema_instruction.field.default is not PydanticUndefined:
            model.other_attributes[alter_schema_instruction.name] = alter_schema_instruction.field.default
    else:
        # Regular field handling
        field = PydanticFieldWrapper(
            alter_schema_instruction.field, alter_schema_instruction.field.annotation, alter_schema_instruction.name
        )
        model.fields[alter_schema_instruction.name] = field
        model.annotations[alter_schema_instruction.name] = alter_schema_instruction.field.annotation


def _change_field_in_model(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    alter_schema_instruction: Union[FieldHadInstruction, FieldDidntHaveInstruction],
    version_change_name: str,
):
    defined_annotations = model._get_defined_annotations_through_mro(schemas)
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name not in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to change the field "{alter_schema_instruction.name}" from '
            f'"{model.name}" in "{version_change_name}" but it doesn\'t have such a field.',
        )

    field = defined_fields[alter_schema_instruction.name]
    model.fields[alter_schema_instruction.name] = field
    model.annotations[alter_schema_instruction.name] = defined_annotations[alter_schema_instruction.name]

    if isinstance(alter_schema_instruction, FieldHadInstruction):
        # TODO: This naming sucks
        _change_field(
            model,
            alter_schema_instruction,
            version_change_name,
            defined_annotations,
            field,
            model.annotations[alter_schema_instruction.name],
        )
    else:
        _delete_field_attributes(
            model,
            alter_schema_instruction,
            version_change_name,
            field,
            model.annotations[alter_schema_instruction.name],
        )


def _change_field(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
    annotation: Union[Any, None],
):
    if alter_schema_instruction.type is not Sentinel:
        if field.annotation == alter_schema_instruction.type:
            raise InvalidGenerationInstructionError(
                f'You tried to change the type of field "{alter_schema_instruction.name}" to '
                f'"{alter_schema_instruction.type}" from "{model.name}" in "{version_change_name}" '
                f'but it already has type "{field.annotation}"',
            )
        field.annotation = alter_schema_instruction.type
        model.annotations[alter_schema_instruction.name] = alter_schema_instruction.type

    if alter_schema_instruction.new_name is not Sentinel:
        if alter_schema_instruction.new_name == alter_schema_instruction.name:
            raise InvalidGenerationInstructionError(
                f'You tried to change the name of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already has that name.",
            )
        model.fields[alter_schema_instruction.new_name] = model.fields.pop(alter_schema_instruction.name)
        model.annotations[alter_schema_instruction.new_name] = model.annotations.pop(
            alter_schema_instruction.name,
            defined_annotations[alter_schema_instruction.name],
        )

    for attr_name in alter_schema_instruction.field_changes.__dataclass_fields__:
        attr_value = getattr(alter_schema_instruction.field_changes, attr_name)
        if attr_value is not Sentinel:
            if field.passed_field_attributes.get(attr_name, Sentinel) == attr_value:
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )
            field.update_attribute(name=attr_name, value=attr_value)


def _delete_field_attributes(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldDidntHaveInstruction,
    version_change_name: str,
    field: PydanticFieldWrapper,
    annotation: Any,
) -> None:
    for attr_name in alter_schema_instruction.attributes:
        deleted = False

        if attr_name in field.passed_field_attributes:
            field.delete_attribute(name=attr_name)
            deleted = True
        if get_origin(annotation) == Annotated and any(  # pragma: no branch
            hasattr(sub_ann, attr_name) for sub_ann in get_args(annotation)
        ):
            for sub_ann in get_args(annotation):
                if hasattr(sub_ann, attr_name):
                    object.__setattr__(sub_ann, attr_name, None)
                    deleted = True
        if not deleted:
            raise InvalidGenerationInstructionError(
                f'You tried to delete the attribute "{attr_name}" of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already doesn't have that attribute.",
            )


def _delete_field_from_model(model: _PydanticModelWrapper[BaseModel], field_name: str, version_change_name: str):
    if field_name in model.fields:
        model.fields.pop(field_name)
        model.annotations.pop(field_name)
        for validator_name, validator in model.validators.copy().items():
            if isinstance(validator, _PerFieldValidatorWrapper) and field_name in validator.fields:
                validator.fields.remove(field_name)
                # TODO: This behavior doesn't feel natural
                if not validator.fields:
                    model.validators[validator_name].is_deleted = True

    elif (
        field_name in model.validators
        and isinstance(model.validators[field_name], _ValidatorWrapper)
        and hasattr(model.validators[field_name], "decorator")
        and model.validators[field_name].decorator == pydantic.computed_field
    ):
        validator = model.validators[field_name]
        model.validators[field_name].is_deleted = True
        model.annotations.pop(field_name, None)
    elif field_name in model.annotations and get_origin(model.annotations[field_name]) is ClassVar:
        # Handle ClassVar fields - they exist in annotations but not in model.fields
        model.annotations.pop(field_name)
        # Also remove the attribute from other_attributes if it exists there
        model.other_attributes.pop(field_name, None)
    else:
        raise InvalidGenerationInstructionError(
            f'You tried to delete a field "{field_name}" from "{model.name}" '
            f'in "{version_change_name}" but it doesn\'t have such a field.',
        )


class _DummyEnum(Enum):
    pass


@final
class _EnumWrapper(Generic[_T_ENUM]):
    __slots__ = "cls", "members", "name"

    def __init__(self, cls: type[_T_ENUM]) -> None:
        self.cls = _unwrap_model(cls)
        self.name = cls.__name__
        self.members = {member.name: member.value for member in cls}

    def __deepcopy__(self, memo: Any) -> "_EnumWrapper[_T_ENUM]":
        result = _EnumWrapper(self.cls)
        result.members = self.members.copy()
        memo[id(self)] = result
        return result

    def generate_model_copy(self, generator: "SchemaGenerator") -> type[_T_ENUM]:
        enum_dict = Enum.__prepare__(self.name, self.cls.__bases__)

        raw_member_map = {k: v.value if isinstance(v, Enum) else v for k, v in self.members.items()}
        initialization_namespace = self._get_initialization_namespace_for_enum(self.cls) | raw_member_map
        for attr_name, attr in initialization_namespace.items():
            enum_dict[attr_name] = attr
        enum_dict["__doc__"] = self.cls.__doc__
        model_copy = cast("type[_T_ENUM]", type(self.name, self.cls.__bases__, enum_dict))
        model_copy.__cadwyn_original_model__ = self.cls  # ty: ignore[unresolved-attribute]
        return model_copy

    @staticmethod
    def _get_initialization_namespace_for_enum(enum_cls: type[Enum]) -> dict[str, Any]:
        mro_without_the_class_itself = enum_cls.mro()[1:]

        mro_dict = {}
        for cls in reversed(mro_without_the_class_itself):
            mro_dict.update(cls.__dict__)

        return {
            k: v
            for k, v in enum_cls.__dict__.items()
            if k not in enum_cls._member_names_
            and k not in _DummyEnum.__dict__
            and (k not in mro_dict or mro_dict[k] is not v)
        }


def _is_same_parametrization(model: type[Union[BaseModel, Enum]], generic: type[BaseModel]) -> bool:
    """Whether the model is the generic or a parametrization of the same origin with the same args.

    E.g. Page[Category] and Page["Category"] are different classes but they mean the same thing
    """
    if model is generic:
        return True
    origin = generic.__pydantic_generic_metadata__["origin"]
    return (
        lenient_issubclass(model, BaseModel)
        and model.__pydantic_generic_metadata__["origin"] is origin
        and _eval_generic_args(model) == _eval_generic_args(generic)
    )


def _eval_generic_args(model: type[BaseModel]) -> tuple[Any, ...]:
    # Generic arguments such as "Item" in Page["Item"] stay strings in pydantic's generic metadata
    return tuple(_eval_forward_refs(arg, model) for arg in model.__pydantic_generic_metadata__["args"])


def _eval_forward_refs(annotation: Any, model: type[BaseModel]) -> Any:
    """Resolve the forward refs in the annotation of a model the way pydantic does, keeping the unresolvable ones"""
    if not _contains_forward_refs(annotation):
        return annotation
    new_annotation, _ = pydantic_try_eval_type(
        annotation,
        getattr(sys.modules.get(model.__module__), "__dict__", None),
        {model.__name__: model} | (unpack_lenient_weakvaluedict(model.__pydantic_parent_namespace__) or {}),
    )
    return new_annotation


def _contains_forward_refs(annotation: Any) -> bool:
    return isinstance(annotation, str | ForwardRef) or any(_contains_forward_refs(arg) for arg in get_args(annotation))


def _try_eval_type(value: Any, globals: dict[str, Any]) -> Any:
    new_value, success = pydantic_try_eval_type(value, globals)
    if success:
        return new_value
    else:
        return value
