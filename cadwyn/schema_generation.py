import collections.abc
import copy
import dataclasses
import functools
import inspect
import operator
import sys
import threading
import types
import typing
from collections.abc import Callable, Sequence
from datetime import date
from enum import Enum, auto
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

import annotated_types
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


class _FieldAttributeChange(NamedTuple):
    changed_by: type[BaseModel]
    name: str
    value: Any
    is_deleted: bool


@dataclasses.dataclass(**DATACLASS_SLOTS)
class PydanticFieldWrapper:
    """We DO NOT maintain field.metadata at all"""

    init_model_field: dataclasses.InitVar[FieldInfo]

    annotation: Any
    name_from_newer_version: str

    # The attributes after the changes of every model that shares this field: the model that defines it and the
    # subclasses that changed it. We keep using them to validate the instructions so that we accept the same ones.
    passed_field_attributes: dict[str, Any] = dataclasses.field(init=False)
    declared_field_attributes: dict[str, Any] = dataclasses.field(init=False)
    changes: list[_FieldAttributeChange] = dataclasses.field(init=False)
    models_that_changed_it: set[type[BaseModel]] = dataclasses.field(init=False)
    _field_attributes_by_model: dict[type[BaseModel], dict[str, Any]] = dataclasses.field(init=False, repr=False)

    def __post_init__(self, init_model_field: FieldInfo) -> None:
        self.passed_field_attributes = _extract_passed_field_attributes(init_model_field)
        self.declared_field_attributes = self.passed_field_attributes.copy()
        self.changes = []
        self.models_that_changed_it = set()
        self._field_attributes_by_model = {}

    def update_attribute(self, *, name: str, value: Any, changed_by: type[BaseModel]) -> None:
        self.passed_field_attributes[name] = value
        self.changes.append(_FieldAttributeChange(changed_by, name, value, is_deleted=False))
        self.models_that_changed_it.add(changed_by)
        self._field_attributes_by_model.clear()

    def delete_attribute(self, *, name: str, changed_by: type[BaseModel]) -> None:
        self.passed_field_attributes.pop(name, None)
        self.changes.append(_FieldAttributeChange(changed_by, name, None, is_deleted=True))
        self.models_that_changed_it.add(changed_by)
        self._field_attributes_by_model.clear()

    def get_field_attributes(self, model: type[BaseModel]) -> dict[str, Any]:
        """Return the attributes of the field in the model: only the changes of the model and its parents apply.

        So the changes that a subclass makes to an inherited field do not change its parents and their other subclasses.
        """
        if model not in self._field_attributes_by_model:
            self._field_attributes_by_model[model] = self._apply_changes(model)
        return self._field_attributes_by_model[model]

    def _apply_changes(self, model: type[BaseModel]) -> dict[str, Any]:
        applies = {changed_by: issubclass(model, changed_by) for changed_by in self.models_that_changed_it}
        if all(applies.values()):
            return self.passed_field_attributes
        attributes = self.declared_field_attributes.copy()
        for change in self.changes:
            if not applies[change.changed_by]:
                continue
            if change.is_deleted:
                attributes.pop(change.name, None)
            else:
                attributes[change.name] = change.value
        return attributes


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


_UNRESOLVED: Any = object()
_T_EVALUATED = TypeVar("_T_EVALUATED")


def _evaluate_user_annotations(evaluate: Callable[[], _T_EVALUATED]) -> _T_EVALUATED:
    """Evaluate the annotations of the user's code for reference discovery, returning _UNRESOLVED if they fail.

    Discovery evaluates the annotations of models that main may never copy, e.g. the ones in untouched generic args.
    Any exception from evaluating them, such as a NameError for a name that is not defined yet, a TypeError for
    a generic that only exists in stubs (`pandas.Series[int]`, Django's `QuerySet[Model]`) or an AttributeError,
    means that discovery cannot know which models copying would request. The generic args that lead to them then
    keep their head models, which is exactly main's behavior, so catching every exception here is safe. Only the
    evaluation of the user's annotations goes through here so the bugs in Cadwyn's own logic still surface.
    """
    try:
        return evaluate()
    except RecursionError:
        # This is more likely a bug in Cadwyn than in the user's annotations
        raise
    except Exception:  # noqa: BLE001
        return _UNRESOLVED


def _has_undefined_names_in_lazy_annotations(obj: Any) -> bool:
    """Whether the annotations of a class or a function refer to names that are not defined yet.

    Since Python 3.14, annotations are evaluated lazily and _get_annotations_without_evaluating keeps such names
    as forward refs, which would resolve to other objects once the names get defined. Earlier versions evaluate
    annotations when they are defined unless they are strings, which stay strings.
    """
    if sys.version_info >= (3, 14):  # pragma: no cover
        from annotationlib import Format, get_annotations

        return _evaluate_user_annotations(lambda: get_annotations(obj, format=Format.VALUE)) is _UNRESOLVED
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
        name: _rebuild_annotated(name, value) if _is_unresolved_annotation(value) else value
        for name, value in _get_annotations_without_evaluating(model).items()
    }


def _is_unresolved_annotation(annotation: Any) -> bool:
    """Whether the annotation is a string or a forward ref that annotationlib created for undefined names.

    Forward refs that the user wrote (e.g. `ForwardRef("Model")` in create_model) stay as they are, as on main.
    """
    if isinstance(annotation, str):
        return True
    return isinstance(annotation, ForwardRef) and getattr(annotation, "__owner__", None) is not None


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
    typevars_map = get_model_typevars_map(model)
    if _REBUILD_MODEL_FIELDS_TAKES_CONFIG:
        config_wrapper = ConfigWrapper(model.model_config, check=False)
        # Pydantic evaluates the annotations of the fields in there
        rebuilt_fields = _evaluate_user_annotations(
            lambda: rebuild_model_fields(
                model, config_wrapper=config_wrapper, ns_resolver=ns_resolver, typevars_map=typevars_map
            )
        )
    else:  # pragma: no cover # Pydantic 2.11
        rebuilt_fields = _rebuild_model_fields_without_changing_them(model, ns_resolver, typevars_map)
    if rebuilt_fields is _UNRESOLVED:
        # E.g. some names in the annotations are not defined yet
        return None
    # Pydantic >= 2.13 also returns the info about the extra fields
    return rebuilt_fields[0] if isinstance(rebuilt_fields, tuple) else rebuilt_fields


def _rebuild_model_fields_without_changing_them(  # pragma: no cover # Pydantic 2.11
    model: type[BaseModel], ns_resolver: NsResolver, typevars_map: Any
) -> Any:
    """Do what rebuild_model_fields of pydantic 2.11 does without changing the fields of the model.

    Pydantic 2.11 merges the `Annotated` metadata of a field into the FieldInfo assigned to the field in place,
    so every call would add that metadata (e.g. validators) to the field of the model once more.
    """
    from pydantic._internal._generics import replace_types
    from pydantic._internal._typing_extra import eval_type
    from pydantic_core import PydanticUndefined
    from typing_inspection.introspection import AnnotationSource

    rebuilt_fields: dict[str, FieldInfo] = {}
    with ns_resolver.push(model):
        for name, field_info in model.__pydantic_fields__.items():
            if field_info._complete:
                rebuilt_fields[name] = field_info
                continue
            types_namespace = ns_resolver.types_namespace
            evaluated_annotation = _evaluate_user_annotations(
                lambda field_info=field_info, types_namespace=types_namespace: eval_type(
                    field_info._original_annotation, *types_namespace
                )
            )
            if evaluated_annotation is _UNRESOLVED:
                return _UNRESOLVED
            annotation = replace_types(evaluated_annotation, typevars_map)
            assignment = field_info._original_assignment
            if assignment is PydanticUndefined:
                rebuilt_fields[name] = FieldInfo.from_annotation(annotation, _source=AnnotationSource.CLASS)
                continue
            if isinstance(assignment, FieldInfo):
                assignment = copy.copy(assignment)
                assignment.metadata = list(assignment.metadata)
            rebuilt_fields[name] = FieldInfo.from_annotated_attribute(
                annotation, assignment, _source=AnnotationSource.CLASS
            )
    return rebuilt_fields


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
            # Subclasses can share fields with their parents so we copy each of them once
            fields=copy.deepcopy(self.fields, memo),
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
        return _change_version_of_model_references(generator, self.cls, self.get_field_attributes(), self.annotations)

    def get_field_attributes(self) -> dict[str, dict[str, Any]]:
        """Return the attributes of the fields that the copy of the model defines (`cadwyn render` uses them too)"""
        return {name: field.get_field_attributes(self.cls) for name, field in self.fields.items()}

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


def _unwrap_partial(call: Any) -> Any:
    # The same as FastAPI's _impartial(), which decides how FastAPI calls the original dependency
    while isinstance(call, functools.partial):
        call = call.func
    return call


def _get_function_behind_callable(call: Any) -> Any:
    """Return the function that runs when the callable is called. It decides how FastAPI must call the callable.

    Partials only pre-fill arguments so we unwrap them. We do not follow decorators (__wrapped__) because the outer
    decorator is what runs. Classes are returned as is because calling them constructs an instance.
    """
    call = _unwrap_partial(call)
    if is_regular_function(call) or inspect.isclass(call):
        return call
    return call.__call__


def _get_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its postponed (string) annotations evaluated, like FastAPI does.

    inspect.signature() itself evaluates them in the globals of whichever function it reads the signature from,
    which is the only reliable way to pick the right globals for every kind of wrapper (partials, decorators, etc).
    It evaluates them only once so the forward references within them (e.g. `body: "Model"` or `list["Model"]`
    with postponed annotations) are left.
    """
    try:
        return inspect.signature(call, eval_str=True)
    # This is deliberately narrow: an annotation may use a name that only exists for type checkers
    # (e.g. one imported under "if TYPE_CHECKING:"). FastAPI handles this the same way. The parameter annotations
    # that can be resolved individually are resolved later in _modify_callable_annotations
    except NameError:
        return _get_unevaluated_signature(call)


def _get_unevaluated_signature(call: Callable[..., Any]) -> inspect.Signature:
    """Return the signature of the callable with its annotations as written, e.g. strings for postponed ones"""
    if sys.version_info >= (3, 14):
        from annotationlib import Format

        # Unresolvable names in lazily evaluated annotations become ForwardRefs instead of raising
        return inspect.signature(call, annotation_format=Format.FORWARDREF)
    return inspect.signature(call)


_PARTIAL_DICT_ACCESSOR = vars(functools.partial)["__dict__"]


def _get_best_effort_globals(call: Any) -> dict[str, Any]:
    """Return the globals of the callable for the string annotations that _get_signature() left.

    Like FastAPI, we take them from the unwrapped callable. Unlike FastAPI, we also unwrap partials that do not
    carry globals themselves. For callable class instances, __globals__ is on the __call__ method instead.
    Partial subclasses that override __call__ are treated like other callable instances, just like Cadwyn always did.
    """
    unwrapped_call = inspect.unwrap(call)
    while True:
        is_partial_with_overridden_call = isinstance(unwrapped_call, functools.partial) and (
            type(unwrapped_call).__call__ is not functools.partial.__call__
        )
        # Proxies can pass the isinstance() check by faking __class__ but they have no partial __dict__ to create
        if is_partial_with_overridden_call and issubclass(type(unwrapped_call), functools.partial):
            # This has nothing to do with globals but it must stay: accessing the instance __dict__ of a partial
            # creates it. Python 3.13+ flattens a partial of a partial (e.g. the one Starlette's run_in_threadpool
            # makes) into a single partial of the inner .func if the inner partial has no __dict__, which would
            # silently skip the overridden __call__ of a partial subclass. Cadwyn used to create the __dict__ as
            # a side effect of inspect.getattr_static(partial, "__call__") so we keep doing it explicitly.
            # We use the accessor of partial itself so that __getattribute__ or __dict__ of subclasses cannot
            # interfere, and we do it before anything else so that it also happens for the subclasses that
            # forward __globals__ to their .func (e.g. through __getattr__)
            _PARTIAL_DICT_ACCESSOR.__get__(unwrapped_call)
        unwrapped_call_globals = getattr(unwrapped_call, "__globals__", None)
        if isinstance(unwrapped_call_globals, dict):
            return unwrapped_call_globals
        if not isinstance(unwrapped_call, functools.partial) or is_partial_with_overridden_call:
            break
        unwrapped_call = inspect.unwrap(unwrapped_call.func)
    dunder_call = inspect.getattr_static(unwrapped_call, "__call__", None)
    return _function_globals(dunder_call) if is_regular_function(dunder_call) else {}


def _copy_field_info(field_info: FieldInfo) -> FieldInfo:
    # The same as pydantic's FieldInfo._copy() (which does not exist in older versions of pydantic):
    # the mutable attributes must not be shared with the original either
    field_info_copy = copy.copy(field_info)
    for attr_name in ("metadata", "_attributes_set", "_qualifiers"):
        setattr(field_info_copy, attr_name, copy.copy(getattr(field_info, attr_name)))
    return field_info_copy


class _CallableKind(Enum):
    ENDPOINT = auto()
    DEPENDENCY = auto()
    # Callables that FastAPI never calls itself, e.g. pydantic's default factories or callables in Annotated metadata
    OTHER = auto()


class _CallableWrapper:
    __annotations__: dict[str, Any]
    __defaults__: Union[tuple[Any, ...], None]
    __signature__: inspect.Signature
    # Whether it wraps an endpoint or a dependency rather than a callable that FastAPI never calls itself
    # (e.g. pydantic's default factories). The latter are wrapped exactly the way Cadwyn always did
    _is_called_by_fastapi = False

    # __eq__ and __hash__ are needed to make sure that dependency overrides work correctly.
    # They are based on putting dependencies (functions) as keys for the dictionary so if we want to be able to
    # override the wrapper, we need to make sure that it is equivalent to the original in __hash__ and __eq__

    def __init__(self, original_callable: Callable[..., Any], *, is_called_by_fastapi: bool) -> None:
        super().__init__()
        self._original_callable = original_callable
        if is_called_by_fastapi:
            self._is_called_by_fastapi = True
            # FastAPI follows __wrapped__ to find out how to call a dependency so it must lead to the same place
            # as it would for the original. We only copy the attributes of functions because copying the ones of
            # classes and their instances would make us look like them to FastAPI
            wrapped = _unwrap_partial(original_callable)
            functools.update_wrapper(
                self, wrapped, updated=functools.WRAPPER_UPDATES if is_regular_function(wrapped) else ()
            )
        else:
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

    def __deepcopy__(self, memo: dict[int, Any]) -> "_CallableWrapper":
        if not self._is_called_by_fastapi:
            # The same as the default deepcopy, which is what Cadwyn always did for the other callables
            wrapper_copy = object.__new__(type(self))
            memo[id(self)] = wrapper_copy
            wrapper_copy.__dict__.update(copy.deepcopy(self.__dict__, memo))
            return wrapper_copy
        # Copying would also copy the original dependency (e.g. a class instance or a partial) so it would stop
        # matching the keys of app.dependency_overrides and changes to its state would not be visible in the copy.
        # Wrappers are never mutated after creation and every version creates its own wrappers anyway.
        return self

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
    def __call__(self, *args: Any, **kwargs: Any) -> typing.Iterator[Any]:
        # "yield from" forwards send(), throw(), and close() so the wrapped generator handles exceptions itself
        yield from self._original_callable(*args, **kwargs)


class _AsyncGeneratorCallableWrapper(_CallableWrapper):
    @override
    async def __call__(self, *args: Any, **kwargs: Any) -> typing.AsyncGenerator[Any, Any]:
        # There is no "yield from" for async generators so we forward asend() and athrow() by hand.
        # Otherwise, exceptions (e.g. the ones raised in the endpoint) would never reach the wrapped generator
        # so its "except" and "finally" blocks would get skipped until it gets garbage collected.
        # aclose() is forwarded too because it throws GeneratorExit into us.
        # The exception is forwarded outside of the "except" block. Otherwise, it would still be the one being handled
        # while the wrapped generator runs which would change sys.exc_info() and the __context__ of its exceptions.
        generator = self._original_callable(*args, **kwargs)
        try:
            value = await generator.__anext__()
            while True:
                sent_value = thrown_exception = None
                try:
                    sent_value = yield value
                except BaseException as e:  # noqa: BLE001
                    thrown_exception = e
                if thrown_exception is None:
                    value = await generator.asend(sent_value)
                else:
                    value = await generator.athrow(thrown_exception)
        except StopAsyncIteration:
            return


@final
class _AnnotationTransformer:
    def __init__(
        self, generator: "Union[SchemaGenerator, _ReferencedModelCollector]", *, record_only: bool = False
    ) -> None:
        self.generator = generator
        # Only request the models from the generator without rebuilding the annotations. This way, we can find
        # the models that an annotation refers to without failing on annotations that we cannot rebuild
        self.record_only = record_only
        # The ids of the type aliases, callables and containers that we are recording, to detect the ones that
        # lead back to themselves (e.g. a default factory whose default argument is a registry that contains it)
        self._ids_being_recorded: set[int] = set()
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
            if self.record_only:
                self._record_without_cycles(annotation, lambda: [*annotation.keys(), *annotation.values()])
                return annotation
            return {
                self.change_version_of_annotation(key): self.change_version_of_annotation(value)
                for key, value in annotation.items()
            }

        elif isinstance(annotation, list | tuple):
            if self.record_only:
                self._record_without_cycles(annotation, lambda: annotation)
                return annotation
            return type(annotation)(self.change_version_of_annotation(v) for v in annotation)
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
        route.endpoint = self.change_versions_of_a_non_container_annotation(
            route.endpoint, callable_kind=_CallableKind.ENDPOINT
        )
        for callback in route.callbacks or []:
            if not isinstance(callback, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(callback, ignore_response_model=ignore_response_model)
        self._remake_endpoint_dependencies(route)

    def _change_version_of_a_non_container_annotation(
        self, annotation: Any, *, callable_kind: _CallableKind = _CallableKind.OTHER
    ) -> Any:
        """callable_kind tells how FastAPI uses the annotation if it is a callable"""
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
            if self.record_only:
                self.change_versions_of_a_non_container_annotation(
                    annotation.dependency, callable_kind=_CallableKind.DEPENDENCY
                )
                return annotation
            # This also covers Security. Copying keeps all of their other fields (e.g. use_cache, scope, and scopes)
            # without calling __init__ which subclasses could have overridden with a different signature
            annotation_copy = copy.copy(annotation)
            dependency = self.change_versions_of_a_non_container_annotation(
                annotation.dependency, callable_kind=_CallableKind.DEPENDENCY
            )
            object.__setattr__(annotation_copy, "dependency", dependency)
            return annotation_copy
        elif isinstance(annotation, UnionType):  # pragma: no cover
            args = tuple(self.change_version_of_annotation(a) for a in get_args(annotation))
            return annotation if self.record_only else typing.Union[args]
        elif is_any(annotation) or is_newtype(annotation):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation)
        elif callable(annotation):
            return self._change_version_of_callable(annotation, callable_kind=callable_kind)
        else:
            return annotation

    def _change_version_of_callable(self, annotation: Callable[..., Any], *, callable_kind: _CallableKind) -> Any:
        if type(annotation).__module__.startswith(
            ("fastapi.", "pydantic.", "pydantic_core.", "starlette.")
        ) or isinstance(annotation, fastapi.security.base.SecurityBase):
            return annotation
        # Special forms such as typing.Self are callable but they are not functions
        if type(annotation).__module__ in {"typing", "typing_extensions"}:
            return annotation
        if callable_kind is _CallableKind.OTHER:
            try:
                _get_signature_parameters(annotation)
            except (ValueError, TypeError):
                # Callables without an inspectable signature such as `functools.partial(dict, count=1)`
                # have no annotations or defaults to version
                return annotation

        def modifier(annotation: Any):
            return self.change_version_of_annotation(annotation)

        if self.record_only:
            self._record_without_cycles(
                annotation, lambda: self._get_annotations_and_defaults_to_record(annotation, callable_kind)
            )
            return annotation
        return self._modify_callable_annotations(
            annotation,
            modifier,
            modifier,
            annotation_modifying_wrapper_factory=functools.partial(
                self._copy_function_through_class_based_wrapper,
                is_called_by_fastapi=callable_kind is not _CallableKind.OTHER,
            ),
            callable_kind=callable_kind,
        )

    def _record_without_cycles(self, obj: Any, get_values_to_record: Callable[[], Any]) -> None:
        """Record the values within the object unless we are already recording it.

        Copying an object that leads back to itself (e.g. `type Json = dict[str, Json] | int`, or a default
        factory whose default argument is a registry that contains the factory) would recurse forever, as it does
        on main, so its references are unknown.
        """
        if id(obj) in self._ids_being_recorded:
            self._record_unknown_references()
            return
        self._ids_being_recorded.add(id(obj))
        try:
            values = get_values_to_record()
            if values is not _UNRESOLVED:
                for value in values:
                    self.change_version_of_annotation(value)
        finally:
            self._ids_being_recorded.discard(id(obj))

    def _get_annotations_and_defaults_to_record(self, call: Callable[..., Any], callable_kind: _CallableKind) -> Any:
        """Return the annotations and the defaults that _modify_callable_annotations versions, or _UNRESOLVED"""
        if callable_kind is _CallableKind.OTHER:
            # The annotations are read from the same wrapper as in _modify_callable_annotations
            wrapper = self._copy_function_through_class_based_wrapper(call, is_called_by_fastapi=False)
            if _has_undefined_names_in_lazy_annotations(wrapper):
                # Copying it fails on Python 3.14, as on main
                self._record_unknown_references()
                return _UNRESOLVED
            callable_annotations, defaults = self._get_callable_annotations_and_defaults(
                call, _get_annotations_without_evaluating(wrapper), _get_signature_parameters(call)
            )
        else:
            signature = _evaluate_user_annotations(lambda: inspect.signature(call, eval_str=True))
            if signature is _UNRESOLVED:
                # E.g. some names are not defined yet so copying could request other models once they get defined
                self._record_unknown_references()
                return _UNRESOLVED
            callable_annotations = self._get_annotations_of_callable_called_by_fastapi(
                call, signature, callable_kind, evaluate=_try_eval_type_for_discovery
            )
            defaults = tuple(
                param.default for param in signature.parameters.values() if param.default is not inspect.Signature.empty
            )
        # The string annotations that cannot be evaluated yet stay strings
        if any(type(value) is str for value in callable_annotations.values()):
            self._record_unknown_references()
        return [callable_annotations, defaults]

    def _record_type_alias(self, alias: Any) -> None:
        # The values of native type aliases are evaluated lazily since Python 3.12
        if _evaluate_user_annotations(lambda: alias.__value__) is _UNRESOLVED:
            self._record_unknown_references()
            return
        self._record_without_cycles(alias, lambda: [_get_type_alias_value(alias), alias.__type_params__])

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
        if origin is Annotated:
            # Annotated[...] is cached based on "==" and "hash" while our dependency wrappers are equal to the
            # originals so it could return the alias that holds the dependency of another version
            return _AnnotatedAlias(new_args[0], new_args[1:])
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
        callable_kind: _CallableKind,
    ) -> _Call:
        annotation_modifying_wrapper = annotation_modifying_wrapper_factory(call)
        if callable_kind is _CallableKind.OTHER:
            # FastAPI never evaluates the annotations of such callables (e.g. pydantic's default factories) and they
            # may not even be evaluable at runtime (e.g. pandas.Series[int]) so this is exactly what Cadwyn always did
            old_params = inspect.signature(call).parameters
            if sys.version_info >= (3, 14):  # pragma: no cover
                from annotationlib import get_annotations

                callable_annotations = get_annotations(annotation_modifying_wrapper)
            else:
                callable_annotations = annotation_modifying_wrapper.__annotations__
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
            annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
            annotation_modifying_wrapper.__defaults__ = modify_defaults(
                tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
            )
        else:
            # FastAPI evaluates the annotations of endpoints and dependencies the same way so we cannot fail where
            # FastAPI would not. Unlike __annotations__, the signature also follows functools.partial and __signature__
            old_signature = _get_signature(call)
            old_params = old_signature.parameters
            return_annotation = old_signature.return_annotation
            callable_annotations = cls._get_annotations_of_callable_called_by_fastapi(
                call, old_signature, callable_kind
            )
            annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
            if callable_kind is _CallableKind.DEPENDENCY and return_annotation is not inspect.Signature.empty:
                # FastAPI never looks at the return annotations of dependencies so we neither evaluate nor version them.
                # Versioning them would generate their models for nothing, which can even fail
                # (e.g. for default factories without a signature such as time.time before Python 3.13)
                annotation_modifying_wrapper.__annotations__["return"] = return_annotation
            annotation_modifying_wrapper.__defaults__ = modify_defaults(
                tuple(
                    # FastAPI mutates the FieldInfo objects in defaults (e.g. their annotation) so every version
                    # (and app) must have its own copies. Otherwise, all of them would use the models of whichever
                    # got built last
                    _copy_field_info(p.default) if isinstance(p.default, FieldInfo) else p.default
                    for p in old_params.values()
                    if p.default is not inspect.Signature.empty
                ),
            )
        annotation_modifying_wrapper.__signature__ = cls._generate_signature(
            annotation_modifying_wrapper,
            old_params,
        )

        return cast("_Call", annotation_modifying_wrapper)

    @classmethod
    def _get_annotations_of_callable_called_by_fastapi(
        cls,
        call: Any,
        old_signature: inspect.Signature,
        callable_kind: _CallableKind,
        *,
        evaluate: Union[Callable[[Any, dict[str, Any]], Any], None] = None,
    ) -> dict[str, Any]:
        """Return the annotations of an endpoint or a dependency that its versioned copy versions"""
        callable_annotations = {
            name: param.annotation
            for name, param in old_signature.parameters.items()
            if param.annotation is not inspect.Parameter.empty
        }
        return_annotation = old_signature.return_annotation
        if callable_kind is _CallableKind.ENDPOINT and return_annotation is not inspect.Signature.empty:
            # FastAPI infers the response model of endpoints from their return annotations
            callable_annotations["return"] = return_annotation
        # Cadwyn always evaluated the annotations that were written as strings (e.g. postponed ones) recursively
        # before versioning them. inspect.signature() evaluates them only once so forward references can be left
        # within them (e.g. `body: "Model"` or `list["Model"]` with postponed annotations) or they can stay strings
        # (e.g. if they could not be evaluated or if they come from __signature__). FastAPI would evaluate some of
        # them itself but in the globals that lead to the head models so we finish evaluating them here
        unevaluated_signature = _get_unevaluated_signature(call)
        annotations_written_as_strings = {
            name for name, param in unevaluated_signature.parameters.items() if type(param.annotation) is str
        }
        if type(unevaluated_signature.return_annotation) is str:
            annotations_written_as_strings.add("return")
        call_globals = _get_best_effort_globals(cls._unwrap_callable(call))
        evaluate = evaluate or _try_eval_type
        return {
            k: evaluate(v, call_globals) if k in annotations_written_as_strings else v
            for k, v in callable_annotations.items()
        }

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
            k: v if type(v) is not str else _try_eval_type_for_discovery(v, call_globals)
            for k, v in callable_annotations.items()
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
    def _copy_function_through_class_based_wrapper(cls, call: _Call, *, is_called_by_fastapi: bool) -> _CallableWrapper:
        """Separate from copy_endpoint because endpoints MUST be functions in FastAPI, they cannot be cls instances"""
        call = cls._unwrap_callable(call)
        if is_called_by_fastapi:
            actual_call = _get_function_behind_callable(call)
            if inspect.isclass(actual_call):
                # Like FastAPI, we call classes synchronously because calling them constructs an instance
                return _CallableWrapper(call, is_called_by_fastapi=True)
        elif not is_regular_function(call):
            # This means that the callable is actually an instance of a regular class
            actual_call = call.__call__
        else:
            actual_call = call
        if is_async_gen_callable(actual_call):
            return _AsyncGeneratorCallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)
        elif is_coroutine_callable(actual_call):
            return _AsyncCallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)
        elif is_gen_callable(actual_call):
            return _GeneratorCallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)
        else:
            return _CallableWrapper(call, is_called_by_fastapi=is_called_by_fastapi)

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

        if self._is_enum_without_instructions(model):
            return model
        with self._lock:
            if model not in self.concrete_models:
                wrapper = self._get_wrapper_for_model(model)
                self.concrete_models[model] = wrapper.generate_model_copy(self)
            return cast("type[_T_ANY_MODEL]", self.concrete_models[model])

    def _is_enum_without_instructions(self, model: type[Union[BaseModel, Enum]]) -> bool:
        # Such an enum is the same in every version so we do not copy it. Its members must stay the same objects
        # too: e.g. strict models reject the members of a copy of their enum.
        return lenient_issubclass(model, Enum) and model not in self._changed_models

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
        if not self._changed_models:
            # Nothing has instructions so no generic args need versioning and we do not need to look into them
            return False
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

    field_name = (
        alter_schema_instruction.name
        if alter_schema_instruction.new_name is Sentinel
        else alter_schema_instruction.new_name
    )
    for attr_name in alter_schema_instruction.field_changes.__dataclass_fields__:
        attr_value = getattr(alter_schema_instruction.field_changes, attr_name)
        if attr_value is not Sentinel:
            # Another model that shares the field (e.g. a sibling) could have set the value only for itself
            if (
                field.passed_field_attributes.get(attr_name, Sentinel)
                == field.get_field_attributes(model.cls).get(attr_name, Sentinel)
                == attr_value
            ):
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )
            field.update_attribute(name=attr_name, value=attr_value, changed_by=model.cls)
            model.annotations[field_name] = _change_annotated_metadata(
                model.annotations[field_name],
                functools.partial(_metadata_sets_constraint, attr_name=attr_name),
                functools.partial(_set_metadata_attribute, attr_name=attr_name, attr_value=attr_value),
            )


def _change_annotated_metadata(
    annotation: Any, should_change: Callable[[Any], bool], change: Callable[[Any], Any]
) -> Any:
    """Change the attribute in the annotation's metadata in the same way it is changed in the field.

    Otherwise, the metadata would override the field. For example, both `x: Annotated[str, Field(min_length=1)]`
    and `x: str = Field(min_length=1)` with "from __future__ import annotations" (where the annotation is rebuilt
    from FieldInfo.metadata as `Annotated[str, MinLen(1)]`) keep `min_length=1` in the annotation.
    We change the metadata instead of removing it to preserve its order relative to validators.
    """
    if get_origin(annotation) != Annotated:
        return annotation
    sub_annotations = get_args(annotation)
    return _AnnotatedAlias(
        sub_annotations[0],
        tuple(change(sub_ann) if should_change(sub_ann) else sub_ann for sub_ann in sub_annotations[1:]),
    )


def _metadata_sets_constraint(metadata: Any, attr_name: str) -> bool:
    """Whether the metadata sets the constraint, e.g. annotated_types.MinLen, pydantic.Strict, or Field(min_length=1).

    Only constraints need to be changed in FieldInfo: its other attributes, such as description, are overridden by
    the Field() of the field, and json_schema_extra gets merged with it.
    """
    if isinstance(metadata, FieldInfo):
        return attr_name in collect_known_metadata(metadata.metadata)[0]
    # pydantic also allows constraint classes such as `Annotated[int, Strict]`
    return attr_name in collect_known_metadata([metadata])[0] and (
        inspect.isclass(metadata) or hasattr(metadata, attr_name)
    )


def _metadata_has_attribute(metadata: Any, attr_name: str) -> bool:
    """Whether field().didnt_have() deletes the attribute from the metadata"""
    if isinstance(metadata, FieldInfo):
        return hasattr(metadata, attr_name) or attr_name in _extract_passed_field_attributes(metadata)
    if inspect.isclass(metadata):
        return _metadata_sets_constraint(metadata, attr_name)
    # Only attributes that can be set on the object itself, so not str.title from `Annotated[str, "Some doc"]`
    return hasattr(metadata, attr_name) and (
        hasattr(metadata, "__dict__")
        or any(attr_name in getattr(cls, "__slots__", ()) for cls in type(metadata).__mro__)
    )


def _set_metadata_attribute(metadata: Any, attr_name: str, attr_value: Any) -> Any:
    if isinstance(metadata, FieldInfo):
        field_info = _copy_field_info(metadata)
        field_info.metadata = [
            _set_metadata_attribute(sub_metadata, attr_name, attr_value)
            if _metadata_sets_constraint(sub_metadata, attr_name)
            else sub_metadata
            for sub_metadata in field_info.metadata
        ]
        # pydantic<2.12 also keeps constraints in _attributes_set which it uses to build fields
        if attr_name in field_info._attributes_set:  # pragma: no cover
            field_info._attributes_set[attr_name] = attr_value
        return field_info
    if inspect.isclass(metadata):
        # We never change the class itself and use its instance with the same constraints instead
        constraint = metadata.__new__(metadata)
        for name, value in collect_known_metadata([metadata])[0].items():
            object.__setattr__(constraint, name, value)
    else:
        # We don't use dataclasses.replace because it would call __init__ that subclasses can override
        constraint = copy.copy(metadata)
    object.__setattr__(constraint, attr_name, attr_value)
    return constraint


def _delete_metadata_attribute(metadata: Any, attr_name: str) -> Any:
    if isinstance(metadata, FieldInfo):
        # FieldInfo's attributes get the values of an empty Field()
        field_info = _copy_field_info(metadata)
        field_info.metadata = [
            _delete_metadata_attribute(sub_metadata, attr_name)
            if _metadata_sets_constraint(sub_metadata, attr_name)
            else sub_metadata
            for sub_metadata in field_info.metadata
        ]
        field_info._attributes_set.pop(attr_name, None)
        if attr_name in FieldInfo.__slots__:
            setattr(field_info, attr_name, dict_of_empty_field_info[attr_name])
        return field_info
    return _set_metadata_attribute(metadata, attr_name, _get_unset_metadata_attribute_value(metadata, attr_name))


def _get_unset_metadata_attribute_value(metadata: Any, attr_name: str) -> Any:
    # Grouped metadata can become invalid with None (e.g. annotated_types.Len(min_length=None) fails to iterate)
    # so we use the default value of its attribute from the library class that defines it because
    # the default of a user's subclass can be a constraint itself (e.g. `min_length: int = 2`)
    if isinstance(metadata, annotated_types.GroupedMetadata):
        library_dataclasses = (
            cls
            for cls in type(metadata).__mro__
            if dataclasses.is_dataclass(cls) and cls.__module__.partition(".")[0] in {"annotated_types", "pydantic"}
        )
        return next(
            (
                field.default
                for cls in library_dataclasses
                for field in dataclasses.fields(cls)
                if field.name == attr_name and field.default is not dataclasses.MISSING
            ),
            None,
        )
    # pydantic ignores other constraints that are None
    return None


def _delete_field_attributes(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldDidntHaveInstruction,
    version_change_name: str,
    field: PydanticFieldWrapper,
    annotation: Any,
) -> None:
    for attr_name in alter_schema_instruction.attributes:
        deleted = False

        # Another model that shares the field (e.g. a sibling) could have already deleted it from the shared attributes
        if attr_name in field.passed_field_attributes or attr_name in field.get_field_attributes(model.cls):
            deleted = True
        # Any metadata with such an attribute counts (even if it is None) to keep accepting the same instructions
        if get_origin(annotation) == Annotated and any(
            _metadata_has_attribute(sub_ann, attr_name) for sub_ann in get_args(annotation)[1:]
        ):
            annotation = _change_annotated_metadata(
                annotation,
                functools.partial(_metadata_has_attribute, attr_name=attr_name),
                functools.partial(_delete_metadata_attribute, attr_name=attr_name),
            )
            model.annotations[alter_schema_instruction.name] = annotation
            deleted = True
        if not deleted:
            raise InvalidGenerationInstructionError(
                f'You tried to delete the attribute "{attr_name}" of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already doesn't have that attribute.",
            )
        # Even if only the annotation has it because the subclasses that share the field could still have it
        field.delete_attribute(name=attr_name, changed_by=model.cls)


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
    if sys.version_info < (3, 11):
        annotation = _convert_strings_in_builtin_generics_to_forward_refs(annotation)
    globalns = getattr(sys.modules.get(model.__module__), "__dict__", None)
    localns = {model.__name__: model} | (unpack_lenient_weakvaluedict(model.__pydantic_parent_namespace__) or {})
    evaluated_annotation = _evaluate_user_annotations(lambda: pydantic_try_eval_type(annotation, globalns, localns)[0])
    # The forward refs that are left make discovery keep the head args, like main
    return annotation if evaluated_annotation is _UNRESOLVED else evaluated_annotation


def _contains_forward_refs(annotation: Any) -> bool:
    """Whether the annotation has strings or forward refs where typing evaluates them as types.

    So the strings in the args of Literal and in the metadata of Annotated do not count.
    """
    from typing_inspection.typing_objects import is_annotated, is_literal

    if isinstance(annotation, str | ForwardRef):
        return True
    origin = get_origin(annotation)
    if is_literal(origin):
        return False
    args = get_args(annotation)
    if is_annotated(origin):
        args = args[:1]
    return any(_contains_forward_refs(arg) for arg in args)


def _convert_strings_in_builtin_generics_to_forward_refs(annotation: Any) -> Any:
    """Python 3.10 does not evaluate the strings in the args of builtin generics such as list["Item"].

    Python 3.11+ converts them to forward refs while evaluating, which we do ourselves here.
    """
    from typing_inspection.typing_objects import is_annotated, is_literal

    if isinstance(annotation, types.UnionType):
        # Unlike typing.Union, `X | Y` cannot be rebuilt with copy_with
        return functools.reduce(
            operator.or_, (_convert_strings_in_builtin_generics_to_forward_refs(arg) for arg in annotation.__args__)
        )
    if isinstance(annotation, types.GenericAlias):
        args = tuple(
            ForwardRef(arg) if isinstance(arg, str) else _convert_strings_in_builtin_generics_to_forward_refs(arg)
            for arg in annotation.__args__
        )
        return types.GenericAlias(annotation.__origin__, args)
    origin = get_origin(annotation)
    # Bare aliases such as typing.List have no args to convert
    args = getattr(annotation, "__args__", ())
    if is_literal(origin) or not args or not hasattr(annotation, "copy_with"):
        return annotation
    if is_annotated(origin):
        return _AnnotatedAlias(
            _convert_strings_in_builtin_generics_to_forward_refs(annotation.__origin__), annotation.__metadata__
        )
    return annotation.copy_with(tuple(_convert_strings_in_builtin_generics_to_forward_refs(arg) for arg in args))


def _try_eval_type_for_discovery(value: Any, globals: dict[str, Any]) -> Any:
    """Like _try_eval_type but keeps the string if evaluating it fails in any way"""
    evaluated_value = _evaluate_user_annotations(lambda: _try_eval_type(value, globals))
    return value if evaluated_value is _UNRESOLVED else evaluated_value


def _try_eval_type(value: Any, globals: dict[str, Any]) -> Any:
    new_value, success = pydantic_try_eval_type(value, globals)
    if success:
        return new_value
    else:
        return value
