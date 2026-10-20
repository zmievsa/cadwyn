import collections.abc
import contextlib
import copy
import dataclasses
import functools
import inspect
import itertools
import operator
import sys
import threading
import types
import typing
import warnings
from collections.abc import Callable, Generator, Sequence
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
from pydantic._internal._forward_ref import PydanticRecursiveRef
from pydantic._internal._generics import PydanticGenericMetadata, get_model_typevars_map, replace_types
from pydantic._internal._known_annotated_metadata import collect_known_metadata
from pydantic._internal._model_construction import build_lenient_weakvaluedict, unpack_lenient_weakvaluedict
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
from cadwyn.exceptions import CadwynError, InvalidGenerationInstructionError, SchemaGenerationError
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
from cadwyn.structure.versions import (
    _CADWYN_REQUEST_PARAM_NAME,
    _CADWYN_RESPONSE_PARAM_NAME,
    VersionBundle,
    _restore_model_validation_aliases,
)

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


_field_identities = itertools.count()


@dataclasses.dataclass(**DATACLASS_SLOTS)
class PydanticFieldWrapper:
    """We DO NOT maintain field.metadata at all"""

    init_model_field: dataclasses.InitVar[FieldInfo]

    annotation: Any
    name_from_newer_version: str

    # The attributes after the changes of every model that shares this field: the model that defines it and the
    # subclasses that changed it. We keep using them to validate the instructions so that we accept the same ones.
    passed_field_attributes: dict[str, Any] = dataclasses.field(init=False)
    # The model of the last instruction that changed the type of the field
    type_changed_by: Union[type[BaseModel], None] = dataclasses.field(init=False, default=None)
    declared_field_attributes: dict[str, Any] = dataclasses.field(init=False)
    changes: list[_FieldAttributeChange] = dataclasses.field(init=False)
    # The same for all copies of the field
    identity: int = dataclasses.field(init=False)
    models_that_changed_it: set[type[BaseModel]] = dataclasses.field(init=False)
    _field_attributes_by_model: dict[type[BaseModel], dict[str, Any]] = dataclasses.field(init=False, repr=False)

    def __post_init__(self, init_model_field: FieldInfo) -> None:
        self.passed_field_attributes = _extract_passed_field_attributes(init_model_field)
        self.declared_field_attributes = self.passed_field_attributes.copy()
        self.changes = []
        self.identity = next(_field_identities)
        self.models_that_changed_it = set()
        self._field_attributes_by_model = {}

    def copy_for_model(self, model: type[BaseModel], name: str, memo: dict[int, Any]) -> "PydanticFieldWrapper":
        """Return a deep copy of the field with the attributes that it has in the model and no changes to apply.

        Each model gets its own copy of the field (and of its default values) in every version, as before.
        """
        if _has_default_and_default_factory(self.passed_field_attributes):
            # pydantic would fail to create the field, as it did when every model shared these attributes
            changed_by = next(
                (
                    change.changed_by
                    for change in reversed(self.changes)
                    if change.name in {"default", "default_factory"}
                ),
                None,
            )
            # Otherwise, the declaration has both (pydantic<2.12 accepts some) and pydantic fails as before
            if changed_by is not None:  # pragma: no branch # Pydantic 2.11
                raise InvalidGenerationInstructionError(
                    f'Instructions leave the field "{name}" with both a default and a default_factory, which '
                    "pydantic does not allow. The last instruction that changed one of them is for "
                    f'"{changed_by.__name__}".'
                )
        field_copy = copy.copy(self)
        field_copy.annotation = copy.deepcopy(self.annotation, memo)
        field_copy.passed_field_attributes = {
            attr_name: copy.deepcopy(attr_value, memo)
            for attr_name, attr_value in self.get_field_attributes(model).items()
        }
        field_copy.declared_field_attributes = field_copy.passed_field_attributes
        field_copy.changes = []
        field_copy.models_that_changed_it = set()
        field_copy._field_attributes_by_model = {}
        return field_copy

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
        changes = [change for change in self.changes if applies[change.changed_by]]
        if len(changes) == len(self.changes):
            attributes = self.passed_field_attributes
        else:
            attributes = self.declared_field_attributes.copy()
            for change in changes:
                if change.is_deleted:
                    attributes.pop(change.name, None)
                else:
                    attributes[change.name] = change.value
        return _keep_last_of_default_and_default_factory(attributes, changes)


def _keep_last_of_default_and_default_factory(
    attributes: dict[str, Any], changes: "list[_FieldAttributeChange]"
) -> dict[str, Any]:
    """Field() accepts only one of them so if the model has both, it keeps the one that it got last.

    The model can have both because the changes of its siblings that deleted one of them do not apply to it.
    """
    if not _has_default_and_default_factory(attributes):
        return attributes
    last_set = next(
        (
            change.name
            for change in reversed(changes)
            if not change.is_deleted and change.name in {"default", "default_factory"}
        ),
        None,
    )
    if last_set is None:  # pragma: no cover # Pydantic 2.11
        # The declaration has both (pydantic<2.12 accepts some) so pydantic fails as before
        return attributes
    return {name: value for name, value in attributes.items() if name != _OTHER_DEFAULT_ATTRIBUTE[last_set]}


def _has_default_and_default_factory(attributes: dict[str, Any]) -> bool:
    """Whether Field(**attributes) fails because it has both. Like pydantic, `...` and `None` mean that it has none"""
    return _sets_default_attribute(attributes, "default") and _sets_default_attribute(attributes, "default_factory")


def _sets_default_attribute(attributes: dict[str, Any], attr_name: str) -> bool:
    """Whether Field(**attributes) sets default or default_factory: `default=...` and `default_factory=None` don't"""
    if attr_name not in attributes:
        return False
    value = attributes[attr_name]
    # Identity checks because user defaults can compare in any way (e.g. numpy arrays compare element-wise)
    if attr_name == "default":
        return value is not PydanticUndefined and value is not Ellipsis
    return value is not None


_OTHER_DEFAULT_ATTRIBUTE = {"default": "default_factory", "default_factory": "default"}


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

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ModelBundle":
        memos_of_models: dict[type[BaseModel], dict[int, Any]] = collections.defaultdict(dict)
        result = _ModelBundle(
            enums=copy.deepcopy(self.enums, memo),
            schemas={
                model: wrapper.copy_with_schemas(self.schemas, memos_of_models)
                for model, wrapper in self.schemas.items()
            },
        )
        memo[id(self)] = result
        return result


@dataclasses.dataclass(**DATACLASS_SLOTS, **DATACLASS_KW_ONLY)
class _RuntimeSchemaGenContext:
    current_version: "Union[Version, HeadVersion]"
    models: _ModelBundle


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
    try:
        return versioned_response_model.model_validate(migrated_response.body)
    except pydantic.ValidationError as error:
        # The body has the keys of `model_dump(by_alias=True)`, like the bodies that routes migrate. Like routes,
        # we only use its validation aliases to rescue bodies that fail, and only if the restored body passes.
        # The body fails as it is anyway, so whatever goes wrong while we try to rescue it, we don't swallow
        # anything new: we raise the original error as before.
        try:
            restored_body = _restore_model_validation_aliases(versioned_response_model, migrated_response.body)
            if restored_body is not migrated_response.body:
                return versioned_response_model.model_validate(restored_body)
        except Exception:  # noqa: BLE001, S110
            pass
        raise error from None


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


# The models whose copies do not redefine inherited fields (see _ReferenceCycles.get_redefined_fields)
_NO_REDEFINED_FIELDS: "collections.abc.Mapping[str, Any]" = types.MappingProxyType({})


def _get_model_annotations(
    model: type[BaseModel],
    fields: Union[dict[str, FieldInfo], None] = None,
    redefined_fields: "collections.abc.Mapping[str, Any]" = _NO_REDEFINED_FIELDS,
) -> dict[str, Any]:
    fields = model.model_fields if fields is None else fields

    def _rebuild_annotated(name: str, value: Any):
        if field_info := fields.get(name):
            return _get_annotation_with_metadata(field_info)
        return value  # pragma: no cover

    # Pydantic has already resolved the annotations of the fields so we prefer them over unresolved annotations
    annotations = {
        name: _rebuild_annotated(name, value) if _is_unresolved_annotation(value) else value
        for name, value in _get_annotations_without_evaluating(model).items()
    }
    # The inherited fields that the copy redefines (see _ReferenceCycles.get_redefined_fields)
    return annotations | dict(redefined_fields)


def _is_unresolved_annotation(annotation: Any) -> bool:
    """Whether the annotation is a string or a forward ref that annotationlib created for undefined names.

    Forward refs that the user wrote (e.g. `ForwardRef("Model")` in create_model) stay as they are, as on main.
    """
    if isinstance(annotation, str):
        return True
    return isinstance(annotation, ForwardRef) and getattr(annotation, "__owner__", None) is not None


def _get_model_field_names(
    model: type[BaseModel],
    fields: Union[dict[str, FieldInfo], None] = None,
    redefined_field_names: "collections.abc.Iterable[str]" = (),
) -> list[str]:
    fields = model.model_fields if fields is None else fields
    names = [name for name in _get_annotations_without_evaluating(model) if name in fields]
    return names + [name for name in redefined_field_names if name not in names]


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
    from pydantic._internal._typing_extra import eval_type
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


def _get_head_model_references(
    model: type[BaseModel], redefined_fields: "collections.abc.Mapping[str, Any]" = _NO_REDEFINED_FIELDS
) -> "_ModelReferences":
    """Return the models that the copy of a model without instructions would request from the generator.

    Unlike _wrap_pydantic_model, it does not copy anything from the model because the model may never be copied.
    """
    fields = _get_rebuilt_model_fields(model)
    if fields is None or _has_undefined_names_in_lazy_annotations(model):
        # The models that it refers to are unknown until the missing names get defined
        return _ModelReferences(frozenset(), frozenset(), frozenset(), is_complete=False)
    collector = _ReferencedModelCollector()
    _change_version_of_model_references(
        collector,
        model,
        {
            name: _extract_passed_field_attributes(fields[name])
            for name in _get_model_field_names(model, fields, redefined_fields)
        },
        _get_model_annotations(model, fields, redefined_fields),
    )
    return collector.get_references()


def _change_version_of_model_references(
    generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier]",
    model: type[BaseModel],
    field_attributes: dict[str, dict[str, Any]],
    annotations: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...], dict[str, Any], PydanticGenericMetadata]:
    """Version everything in the model that can refer to other models: field attributes, bases, and annotations.

    Only the annotations are type positions (see _AnnotationTransformer.is_in_hard_position). The field attributes,
    such as default factories, and the generic metadata need the classes themselves, just like the bases.
    """
    transformer = generator.annotation_transformer
    with transformer.hard_position():
        versioned_field_attributes = {
            name: transformer.change_version_of_annotation(attributes) for name, attributes in field_attributes.items()
        }
    # __orig_bases__ keeps bases such as Generic[T] that __bases__ loses so that the copy stays generic
    orig_bases = tuple(
        generator.change_version_of_base(base) if isinstance(base, type) else base
        for base in model.__dict__.get("__orig_bases__", model.__bases__)
    )
    versioned_annotations = transformer.change_version_of_annotation(annotations)
    with transformer.hard_position():
        generic_metadata = generator.change_version_of_generic_metadata(model)
    return versioned_field_attributes, orig_bases, versioned_annotations, generic_metadata


# The modules of the generics whose args are types that pydantic resolves, such as list, dict, Optional, or Callable
_TYPE_POSITION_GENERIC_MODULES = frozenset(
    {"builtins", "collections", "collections.abc", "types", "typing", "typing_extensions"}
)


def _is_type_position_in_generic(origin: Any, index: int) -> bool:
    """Whether the arg of the generic at the index is a type that pydantic resolves if it is a forward ref.

    Other generics, such as the ones with a custom __class_getitem__, may need the classes themselves.
    The args of Literal are values and the metadata of Annotated can be anything.
    """
    from typing_inspection.typing_objects import is_annotated, is_literal

    if is_annotated(origin):
        return index == 0
    if is_literal(origin):
        return False
    return getattr(origin, "__module__", None) in _TYPE_POSITION_GENERIC_MODULES


def _rebuild_generic_alias(annotation: Any, new_args: tuple[Any, ...]) -> Any:
    """Rebuild a generic alias such as list[Item] or Optional[Item] with new args"""
    from typing_inspection.typing_objects import is_unpack

    origin = get_origin(annotation)
    # A bare `typing.Callable` has nothing to version and cannot be rebuilt as `Callable[()]`
    if origin is collections.abc.Callable and not new_args:
        return annotation
    # Classvar and Unpack do not support generic tuple arguments
    if origin is ClassVar or is_unpack(origin):
        return origin[new_args[0]]
    return origin[new_args]


def _wrap_pydantic_model(
    model: type[_T_PYDANTIC_MODEL], redefined_fields: "collections.abc.Mapping[str, Any]" = _NO_REDEFINED_FIELDS
) -> "_PydanticModelWrapper[_T_PYDANTIC_MODEL]":
    _rebuild_model(model)
    decorators = _get_model_decorators(model)
    validators = {}
    for decorator_wrapper in decorators:
        if decorator_wrapper.cls_var_name not in model.__dict__:
            continue

        wrapped_validator = _wrap_validator(decorator_wrapper.func, decorator_wrapper.shim, decorator_wrapper.info)
        validators[decorator_wrapper.cls_var_name] = wrapped_validator

    annotations = _get_model_annotations(model, redefined_fields=redefined_fields)
    fields = {
        field_name: PydanticFieldWrapper(
            model.model_fields[field_name],
            annotations[field_name],
            field_name,
        )
        for field_name in _get_model_field_names(model, redefined_field_names=redefined_fields)
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
    fields_renamed_by_parents: Annotated[
        dict["_FieldName", PydanticFieldWrapper],
        Doc(
            "The fields of this model that its parents renamed, by their new names. The model keeps them under their "
            "old names too, as before, and defines them under their new names so that its changes apply to them."
        ),
    ] = dataclasses.field(default_factory=dict, repr=False)
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

    def copy_with_schemas(
        self,
        schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
        memos_of_models: dict[type[BaseModel], dict[int, Any]],
    ) -> "_PydanticModelWrapper[_T_PYDANTIC_MODEL]":
        """Copy the model, sharing copies of its fields' objects as copy.deepcopy(model.fields) did for each model.

        So the fields of the model share the copies of the objects that they share. The fields that the model's
        parents renamed share them with the copy of the parent that the model would inherit them from instead.
        """
        memo_of_model = memos_of_models[self.cls]
        fields_renamed_by_parents = {}
        for name, field in self.fields_renamed_by_parents.items():
            parent = _get_parent_that_renamed_field(self.cls, name, field, schemas)
            memo = {} if parent is None else memos_of_models[parent]
            fields_renamed_by_parents[name] = field.copy_for_model(self.cls, name, memo)
        return _PydanticModelWrapper(
            self.cls,
            name=self.name,
            doc=self.doc,
            fields={name: field.copy_for_model(self.cls, name, memo_of_model) for name, field in self.fields.items()},
            validators=copy.deepcopy(self.validators),
            other_attributes=copy.deepcopy(self.other_attributes),
            annotations=copy.deepcopy(self.annotations),
            fields_renamed_by_parents=fields_renamed_by_parents,
        )

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

    def get_references(self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]") -> "_ModelReferences":
        """Return the models that generate_model_copy requests from the generator"""
        collector = _ReferencedModelCollector()
        self._change_version_of_references(collector, schemas)
        return collector.get_references()

    def _change_version_of_references(
        self,
        generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier]",
        schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    ) -> tuple[dict[str, dict[str, Any]], tuple[Any, ...], dict[str, Any], PydanticGenericMetadata]:
        return _change_version_of_model_references(
            generator, self.cls, *self.get_field_attributes_and_annotations(schemas)
        )

    def get_field_attributes_and_annotations(
        self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Return the attributes of the fields that the copy of the model defines and its annotations.

        Both the copies of the model and `cadwyn render` use them.
        """
        renamed_fields = {}
        renamed_field_annotations = {}
        for name, field in self.fields_renamed_by_parents.items():
            parent = _get_parent_that_renamed_field(self.cls, name, field, schemas)
            if parent is not None:
                renamed_fields[name] = field
                # The annotation that the model would inherit
                renamed_field_annotations[name] = schemas[parent].annotations[name]
        fields = renamed_fields | self.fields
        field_attributes = {name: field.get_field_attributes(self.cls) for name, field in fields.items()}
        annotations = renamed_field_annotations | self.annotations
        for name, attributes in field_attributes.items():
            for attr_name, other_attr_name in _OTHER_DEFAULT_ATTRIBUTE.items():
                if _sets_default_attribute(attributes, attr_name) and not _sets_default_attribute(
                    attributes, other_attr_name
                ):
                    # The Field() in the annotation can still set the other one and pydantic would fail with both,
                    # e.g. with `Annotated[int, Field(default=0)]` (pydantic<2.12 also fills it with the declared
                    # default) after `had(default=..., default_factory=f)` or after a sibling deleted the default
                    annotations[name] = _change_annotated_metadata(
                        annotations[name],
                        lambda metadata, other_attr_name=other_attr_name: (
                            isinstance(metadata, FieldInfo) and other_attr_name in metadata._attributes_set
                        ),
                        functools.partial(_delete_metadata_attribute, attr_name=other_attr_name),
                    )
        return field_attributes, annotations

    def generate_model_copy(self, generator: "Union[SchemaGenerator, _CycleCopier]") -> type[_T_PYDANTIC_MODEL]:
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
        field_attributes, orig_bases, annotations, generic_metadata = self._change_version_of_references(
            generator, generator.model_bundle.schemas
        )
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


def _get_globals(call: Any) -> dict[str, Any]:
    # For callable class instances, __globals__ is on the __call__ method, not on the instance itself
    if is_regular_function(call):
        return _function_globals(call)
    dunder_call = inspect.getattr_static(call, "__call__", None)
    return _function_globals(dunder_call) if is_regular_function(dunder_call) else {}


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
        self,
        generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier]",
        *,
        record_only: bool = False,
        expands_type_aliases: bool = False,
    ) -> None:
        self.generator = generator
        # Only request the models from the generator without rebuilding the annotations. This way, we can find
        # the models that an annotation refers to without failing on annotations that we cannot rebuild
        self.record_only = record_only
        # Record the values of type aliases with their args instead of the args themselves (record-only), so that
        # the args that the values do not use are not recorded (see SchemaGenerator.dependency_differs_from_head)
        self._expands_type_aliases = expands_type_aliases
        # The type aliases whose values we are recording right now (by their ids) with the args of each expansion,
        # to stop at the ones that refer to themselves
        self._type_aliases_being_expanded: dict[int, list[tuple[Any, ...]]] = {}
        # The ids of the type aliases, callables and containers that we are recording, to detect the ones that
        # lead back to themselves (e.g. a default factory whose default argument is a registry that contains it)
        self._ids_being_recorded: set[int] = set()
        # Whether we are recording the routes, whose endpoints and dependencies copying reads like _get_signature
        self._records_routes = False
        # How many positions that need the classes themselves we are in right now (see is_in_hard_position)
        self._hard_position_depth = 0
        self.change_versions_of_a_non_container_annotation = self._change_version_of_a_non_container_annotation
        # This cache cannot be used until something is done regarding https://github.com/fastapi/fastapi/pull/14320
        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/FastAPI do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        # self.change_versions_of_a_non_container_annotation = functools.cache(
        #     self._change_version_of_a_non_container_annotation,
        # )

    @property
    def is_in_hard_position(self) -> bool:
        """Whether the annotation that we are versioning right now must use the classes themselves.

        In type positions, such as the annotations of fields and the args of list or Optional within them, pydantic
        also accepts forward refs to models that it resolves later. Everywhere else, e.g. in default factories,
        the metadata of Annotated, or the args of unknown generics, the classes themselves are needed. Copying
        reference cycles relies on this distinction (see _CycleCopier).
        """
        return self._hard_position_depth > 0

    @contextlib.contextmanager
    def hard_position(self) -> Generator[None, None, None]:
        self._hard_position_depth += 1
        try:
            yield
        finally:
            self._hard_position_depth -= 1

    def change_version_of_annotation(self, annotation: Any) -> Any:
        """Recursively go through all annotations and change them to annotations corresponding to the version passed.

        So if we had a annotation "UserResponse" from "head" version, and we passed version of "2022-11-16", it would
        replace "UserResponse" with the same class but from the "2022-11-16" version.

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

    def record_route(self, route: fastapi.routing.APIRoute) -> None:
        """Request the models that migrate_route_to_version requests without changing the route (record-only)"""
        records_routes = self._records_routes
        self._records_routes = True
        try:
            self._record_route(route)
        finally:
            self._records_routes = records_routes

    def record_dependency(self, dependency: Callable[..., Any]) -> None:
        """Request the models that versioning the dependency requests without changing it (record-only)"""
        records_routes = self._records_routes
        self._records_routes = True
        try:
            self.change_versions_of_a_non_container_annotation(dependency, callable_kind=_CallableKind.DEPENDENCY)
        finally:
            self._records_routes = records_routes

    def _record_route(self, route: fastapi.routing.APIRoute) -> None:
        if route.response_model is not None:
            self.change_version_of_annotation(route.response_model)
        self.change_version_of_annotation(route.dependencies)
        self.change_versions_of_a_non_container_annotation(route.endpoint, callable_kind=_CallableKind.ENDPOINT)
        for callback in route.callbacks or []:
            if isinstance(callback, fastapi.routing.APIRoute):
                self._record_route(callback)

    def _change_version_of_a_non_container_annotation(
        self, annotation: Any, *, callable_kind: _CallableKind = _CallableKind.OTHER
    ) -> Any:
        """callable_kind tells how FastAPI uses the annotation if it is a callable"""
        from typing_inspection.typing_objects import is_any, is_newtype, is_typealiastype

        if self._expands_type_aliases and self._record_expanded_type_alias(annotation):
            return annotation
        if isinstance(annotation, GenericAliasUnionArgs):
            return self._change_version_of_generic_alias(annotation)
        elif is_typealiastype(annotation):
            if (
                annotation.__module__ is not None and (annotation.__module__.startswith("pydantic."))
            ) or annotation.__name__ in _PYDANTIC_ALL_EXPORTED_NAMES:
                return annotation
            # Pydantic resolves the forward refs in the values of type aliases in their own module
            with self.hard_position():
                if self.record_only:
                    self._record_type_alias(annotation)
                    return annotation
                value = self.change_version_of_annotation(_get_type_alias_value(annotation))
                type_params = self.change_version_of_annotation(annotation.__type_params__)
            return type(annotation)(name=annotation.__name__, value=value, type_params=type_params)
        elif isinstance(annotation, fastapi.params.Depends):
            with self.hard_position():
                if self.record_only:
                    self.change_versions_of_a_non_container_annotation(
                        annotation.dependency, callable_kind=_CallableKind.DEPENDENCY
                    )
                    return annotation
                # This also covers Security. Copying keeps all of their other fields (e.g. use_cache, scope, and
                # scopes) without calling __init__ which subclasses could have overridden with a different signature
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
        # Callables, such as default factories, are called with the classes themselves and their annotations are
        # not resolved by pydantic
        with self.hard_position():
            if type(annotation).__module__.startswith(
                ("fastapi.", "pydantic.", "pydantic_core.", "starlette.")
            ) or isinstance(annotation, fastapi.security.base.SecurityBase):
                return annotation
            # Special forms such as typing.Self are callable but they are not functions
            if type(annotation).__module__ in {"typing", "typing_extensions"}:
                return annotation
            if callable_kind is _CallableKind.OTHER:
                try:
                    # A custom __signature__ can also fail when its parameters are read
                    _get_unevaluated_signature(annotation).parameters  # noqa: B018
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

    def _record_expanded_type_alias(self, annotation: Any) -> bool:
        """Record the value of the type alias with its args, if the annotation is a type alias that we can expand.

        E.g. `Unused[Role]` with `type Unused[T] = str` records `str`, and `Alias` with `type Alias = Unused[Role]`
        records `str` too. A type alias that refers to itself is expanded once. Within its value, a reference to it
        with the same args (e.g. `Tree[Role]` within `type Tree[T] = str | list[Tree[T]]` for `Tree[Role]`) records
        nothing because the expansion that we are in already records all of its types. A reference to it with other
        args is recorded with these args, as usual, and so are the aliases that we cannot expand (e.g. with names
        that are not defined yet or with a TypeVarTuple).
        """
        value = _get_value_of_type_alias_with_args(annotation)
        # Pydantic's own type aliases (e.g. JsonValue) are never versioned (see the branch for them below)
        if value is _UNRESOLVED or (value.alias.__module__ or "").startswith("pydantic."):
            return False
        expanded_args = self._type_aliases_being_expanded.get(id(value.alias))
        if expanded_args is not None:
            return any(_are_same_args(value.args, args) for args in expanded_args)
        self._type_aliases_being_expanded[id(value.alias)] = [value.args]
        try:
            self.change_version_of_annotation(value.value)
        finally:
            del self._type_aliases_being_expanded[id(value.alias)]
        return True

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
            callable_annotations = _get_annotations_without_evaluating(wrapper)
            parameters = _get_unevaluated_signature(call).parameters
            call_globals = _get_globals(self._unwrap_callable(call))
            callable_annotations = {
                k: v if type(v) is not str else _try_eval_type_for_discovery(v, call_globals)
                for k, v in callable_annotations.items()
            }
            defaults = tuple(p.default for p in parameters.values() if p.default is not inspect.Signature.empty)
        else:
            # The models of the routes must include every model that versioning the routes requests (see
            # SchemaGenerator._add_route_models) so we read their endpoints and dependencies exactly like copying does,
            # which resolves the annotations one by one if some names are only defined for type checkers. Elsewhere,
            # we keep the decisions that main made about the models that refer to such callables
            get_signature = (
                _get_signature if self._records_routes else functools.partial(inspect.signature, eval_str=True)
            )
            signature = _evaluate_user_annotations(lambda: get_signature(call))
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
        origin = get_origin(annotation)
        args = get_args(annotation)
        new_args = tuple(self._change_version_of_generic_arg(origin, index, arg) for index, arg in enumerate(args))
        if self.record_only:
            return annotation
        if origin is Annotated and self._has_dependency_that_differs_from_head(args):
            # typing caches Annotated[...] by "==" and "hash", and our dependency wrappers are equal to the originals
            # (so that dependency_overrides match them), so typing would usually return the alias that holds
            # the original dependency (#467). We bypass its cache only where the original dependency would validate
            # requests differently, so everywhere else the dependency keeps getting the original classes, as before
            if self._has_inferred_dependency(args) and not isinstance(args[0], type):
                # FastAPI uses the first arg as the dependency (e.g. `Annotated[get_roles, Depends()]`)
                with self.hard_position():
                    dependency = self.change_versions_of_a_non_container_annotation(
                        args[0], callable_kind=_CallableKind.DEPENDENCY
                    )
                new_args = (dependency, *new_args[1:])
            return _AnnotatedAlias(new_args[0], new_args[1:])
        return _rebuild_generic_alias(annotation, new_args)

    def _has_dependency_that_differs_from_head(self, annotated_args: tuple[Any, ...]) -> bool:
        generator = self.generator
        return (
            isinstance(generator, SchemaGenerator)
            and any(
                isinstance(value, fastapi.params.Depends)
                # FastAPI infers the dependency from the type when it is not set (e.g. `Annotated[Params, Depends()]`)
                and generator.dependency_differs_from_head(
                    annotated_args[0] if value.dependency is None else value.dependency
                )
                for value in annotated_args[1:]
            )
        )

    @staticmethod
    def _has_inferred_dependency(annotated_args: tuple[Any, ...]) -> bool:
        return any(
            isinstance(value, fastapi.params.Depends) and value.dependency is None for value in annotated_args[1:]
        )

    def _change_version_of_generic_arg(self, origin: Any, index: int, arg: Any) -> Any:
        if _is_type_position_in_generic(origin, index):
            return self.change_version_of_annotation(arg)
        with self.hard_position():
            return self.change_version_of_annotation(arg)

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
            call_globals = _get_globals(cls._unwrap_callable(call))
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


def _get_subclasses_that_can_be_versioned(model: type[Union[BaseModel, Enum]]) -> tuple[list[type[BaseModel]], bool]:
    """Return the subclasses of the model that make it versioned if they are, and whether we know all of them.

    The parametrizations of a generic (e.g. Page[Item] for Page) do not count because whether they use versioned
    args is decided on its own (see SchemaGenerator._should_version_generic_args) but their subclasses
    (e.g. `class ItemPage(Page[Item])`) do. A class becomes a subclass of its bases before pydantic sets its fields
    and generic metadata (e.g. while another thread parametrizes a generic) so we do not know yet whether it counts.
    """
    if not lenient_issubclass(model, BaseModel):
        return [], True
    subclasses = []
    are_all_known = True
    classes_to_visit = model.__subclasses__()
    while classes_to_visit:
        subclass = classes_to_visit.pop()
        if "__pydantic_fields__" not in vars(subclass):
            are_all_known = False
        elif _is_parametrization(subclass):
            classes_to_visit.extend(subclass.__subclasses__())
        else:
            subclasses.append(subclass)
    # E.g. SQLAlchemy models are never versioned
    return [subclass for subclass in subclasses if _is_versionable_model(subclass)], are_all_known


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

    __slots__ = "annotation_transformer", "arg_models", "base_models", "hard_models", "is_complete", "models"

    def __init__(self, *, expands_type_aliases: bool = False) -> None:
        self.annotation_transformer = _AnnotationTransformer(
            self, record_only=True, expands_type_aliases=expands_type_aliases
        )
        self.models: set[type[Union[BaseModel, Enum]]] = set()
        self.hard_models: set[type[Union[BaseModel, Enum]]] = set()
        self.arg_models: set[type[Union[BaseModel, Enum]]] = set()
        self.base_models: set[type[Union[BaseModel, Enum]]] = set()
        # Whether the annotations that we looked at only refer to names that are already defined
        self.is_complete = True

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(model):
            self.models.add(_unwrap_model(model))
            if self.annotation_transformer.is_in_hard_position:
                self.hard_models.add(_unwrap_model(model))
        return model

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(base):
            self.base_models.add(_unwrap_model(base))
        return base

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
            # A parametrized generic is a subclass of its origin
            self.change_version_of_base(generic_metadata["origin"])
        return generic_metadata

    def get_references(self) -> "_ModelReferences":
        return _ModelReferences(
            frozenset(self.models),
            frozenset(self.arg_models),
            frozenset(self.base_models),
            self.is_complete,
            frozenset(self.hard_models),
        )


class _ModelReferences(NamedTuple):
    # The models that are always versioned along with the model: in its fields, annotations, etc
    models: frozenset[type[Union[BaseModel, Enum]]]
    # The models in the type args of a parametrized generic (e.g. Item in Page[Item])
    arg_models: frozenset[type[Union[BaseModel, Enum]]]
    # The bases of the model and the origin of a parametrized generic (e.g. Page in Page[Item])
    base_models: frozenset[type[Union[BaseModel, Enum]]]
    # Whether we know all of the models that copying the model would request. We do not when some names in its
    # annotations (fields, class variables, quoted generic args, annotations of default factories, values of type
    # aliases, etc.) are not defined yet: once they get defined, copying the model can request any model. We do not
    # either when copying it would recurse forever through a type alias that refers to itself. So we always copy
    # such a model and never version the generic args that lead to it, even after the names get defined, which
    # keeps them exactly as on main.
    is_complete: bool = True
    # The models among `models` that are requested in positions that need the classes themselves rather than
    # forward refs to them, e.g. default factories (see _AnnotationTransformer.is_in_hard_position)
    hard_models: frozenset[type[Union[BaseModel, Enum]]] = frozenset()


class _GenerationAnalysis(NamedTuple):
    # Whether generating the copy of the model will not recurse forever
    terminates: bool
    # Whether the model or the models that it refers to have schema or enum instructions
    refers_to_changed_models: bool


_T_NODE = TypeVar("_T_NODE")
_T_COMPONENT_VALUE = TypeVar("_T_COMPONENT_VALUE")


def _find_strongly_connected_components(
    start: _T_NODE,
    get_successors: Callable[[_T_NODE], "collections.abc.Iterable[_T_NODE]"],
    components: dict[_T_NODE, _T_COMPONENT_VALUE],
    get_value: Callable[[list[_T_NODE], bool], _T_COMPONENT_VALUE],
) -> None:
    """Find the strongly connected components reachable from start (Tarjan's algorithm).

    Maps the nodes of each component in `components` to get_value(component, is_cycle), where is_cycle is whether
    the component has more than one node or a node that is its own successor.
    The nodes already in `components` are skipped because none of them can lead back to the new ones.
    It is iterative instead of recursive because the chains of references in large apps can be very long.
    """
    indices: dict[_T_NODE, int] = {start: 0}
    lowest_indices = {start: 0}
    stack = [start]
    nodes_to_visit = [(start, iter(get_successors(start)))]
    while nodes_to_visit:
        current_node, successors = nodes_to_visit[-1]
        for successor in successors:
            if successor in components:
                continue
            if successor not in indices:
                indices[successor] = lowest_indices[successor] = len(indices)
                stack.append(successor)
                nodes_to_visit.append((successor, iter(get_successors(successor))))
                break
            # The node is still on the stack because the nodes that are not are in finished components
            lowest_indices[current_node] = min(lowest_indices[current_node], indices[successor])
        else:
            nodes_to_visit.pop()
            if nodes_to_visit:
                previous_node = nodes_to_visit[-1][0]
                lowest_indices[previous_node] = min(lowest_indices[previous_node], lowest_indices[current_node])
            if lowest_indices[current_node] == indices[current_node]:
                component = []
                while not component or component[-1] is not current_node:
                    component.append(stack.pop())
                is_cycle = len(component) > 1 or current_node in get_successors(current_node)
                components.update(dict.fromkeys(component, get_value(component, is_cycle)))


@final
class _ReferenceCycles:
    """The reference cycles among the head models, shared by the generators of all versions.

    SchemaGenerator only uses the head models of the cycles whose models are not versioned, and the references of
    such models are the same in every version because only instructions change them. So we find the cycles once
    in the references of the head models. The cycles that only exist in some versions because of instructions
    contain models with instructions so all of their models are versioned and copied anyway.
    """

    __slots__ = (
        "_changed_models",
        "_changed_parametrizations",
        "_head_references",
        "_head_references_with_redefined_fields",
        "_lock",
        "_models_in_cycles",
        "_models_in_cycles_with_redefined_fields",
        "_parametrizations_built_by_pydantic",
        "_reaches_changed_parametrizations",
        "_redefined_field_names",
        "_redefined_fields",
        "_requested_models",
    )

    def __init__(self, changed_models: "frozenset[type[Union[BaseModel, Enum]]]") -> None:
        # The generators of different versions can ask at the same time
        self._lock = threading.Lock()
        self._models_in_cycles: dict[type[Union[BaseModel, Enum]], bool] = {}
        self._requested_models: dict[type[Union[BaseModel, Enum]], frozenset[type[Union[BaseModel, Enum]]]] = {}
        # The references of the head models are the same in every version so the generators share them
        self._head_references: dict[type[BaseModel], _ModelReferences] = {}
        # The models with schema or enum instructions in any version
        self._changed_models = changed_models
        # The parametrizations with instructions, which also apply where pydantic builds them in the fields of other
        # generics, e.g. `schema(Pair[str, int])` in `Box[str]` with `pair: Pair[T, int]` (see
        # get_redefined_field_names). Without them, nothing is redefined.
        self._changed_parametrizations = frozenset(
            model for model in changed_models if lenient_issubclass(model, BaseModel) and _is_parametrization(model)
        )
        # See get_redefined_field_names
        self._parametrizations_built_by_pydantic: dict[
            type[Union[BaseModel, Enum]], dict[str, frozenset[type[BaseModel]]]
        ] = {}
        self._reaches_changed_parametrizations: dict[type[BaseModel], bool] = {}
        self._models_in_cycles_with_redefined_fields: dict[type[Union[BaseModel, Enum]], bool] = {}
        self._redefined_field_names: dict[type[BaseModel], tuple[str, ...]] = {}
        self._redefined_fields: dict[type[BaseModel], collections.abc.Mapping[str, Any]] = {}
        self._head_references_with_redefined_fields: dict[type[BaseModel], _ModelReferences] = {}

    def is_in_cycle(self, model: type[Union[BaseModel, Enum]]) -> bool:
        """Whether copying the model would request the model itself again, directly or through other models.

        Copying the generic args never leads back to the model because they are only versioned when they do not
        (see SchemaGenerator._should_version_generic_args) so only the models in the fields, annotations,
        and bases count.
        """
        with self._lock:
            if model not in self._models_in_cycles:
                _find_strongly_connected_components(
                    model, self._get_requested_models, self._models_in_cycles, lambda _, is_cycle: is_cycle
                )
            return self._models_in_cycles[model]

    def get_requested_models(self, model: type[Union[BaseModel, Enum]]) -> frozenset[type[Union[BaseModel, Enum]]]:
        """Return the head models that copying the head model would request"""
        with self._lock:
            return self._get_requested_models(model)

    def get_head_references(self, model: type[BaseModel]) -> "_ModelReferences":
        """Return the references of the head model (see _get_head_model_references), with its redefined fields"""
        with self._lock:
            redefined_fields = self._get_redefined_fields(model)
            if not redefined_fields:
                return self._get_head_references(model)
            if model not in self._head_references_with_redefined_fields:
                # They are complete because the fields are only redefined if we know what copying them requests
                self._head_references_with_redefined_fields[model] = _get_head_model_references(model, redefined_fields)
            return self._head_references_with_redefined_fields[model]

    def get_redefined_field_names(self, model: type[BaseModel]) -> tuple[str, ...]:
        """Return the inherited fields that the copies of the parametrization of a generic define themselves.

        Pydantic builds the fields that a parametrization (e.g. `Box[str]`) inherits from its generic
        (e.g. `pair: Pair[T, int]` in `Box`) by parametrizing their generics itself, through its own cache. So in
        the copy of `Box[str]`, `pair` gets a `Pair[str, int]` that pydantic builds from the copy of `Pair`
        instead of the copy of `Pair[str, int]` itself, which is the only one that has the instructions of
        `schema(Pair[str, int])`. So when such parametrizations lead to parametrizations with their own
        instructions, the copy defines these fields itself with the annotations that pydantic resolved for the head
        parametrization, which the generator then versions as usual. Everything else stays as on main: the fields
        of the parametrizations whose generic, its bases, or the parametrization itself have instructions (their
        fields can differ from the head ones), and the parametrizations whose redefined fields would lead back to
        them (copying them would never finish).
        """
        with self._lock:
            return self._get_redefined_field_names(model)

    def get_redefined_fields(self, model: type[BaseModel]) -> "collections.abc.Mapping[str, Any]":
        """Return the annotations of the fields that the copies of the model redefine (see get_redefined_field_names).

        They are the annotations that pydantic resolved for the head model, with the parametrizations that have
        instructions instead of the equal ones that pydantic built separately (Pydantic 2.11 builds another
        `Pair[str, int]` for `Box[str]` if `Box[str]` is created before `Pair[str, int]` itself).
        """
        with self._lock:
            return self._get_redefined_fields(model)

    def _get_redefined_fields(self, model: type[BaseModel]) -> "collections.abc.Mapping[str, Any]":
        redefined_field_names = self._get_redefined_field_names(model)
        if not redefined_field_names:
            return _NO_REDEFINED_FIELDS
        if model not in self._redefined_fields:
            # The fields are only redefined if they are known (see _get_parametrizations_built_by_pydantic)
            fields = cast("dict[str, FieldInfo]", _get_rebuilt_model_fields(model))
            self._redefined_fields[model] = types.MappingProxyType(
                {
                    name: _replace_in_type_positions(
                        _get_annotation_with_metadata(fields[name]), self._get_changed_parametrization
                    )
                    for name in redefined_field_names
                }
            )
        return self._redefined_fields[model]

    def _get_changed_parametrization(self, annotation: Any) -> Any:
        """Return the parametrization with instructions that is the same as the annotation, or the annotation"""
        # The type positions can hold anything, including unhashable values such as the list of args of Callable
        if not (lenient_issubclass(annotation, BaseModel) and _is_parametrization(annotation)) or (
            annotation in self._changed_parametrizations
        ):
            return annotation
        return next(
            (
                parametrization
                for parametrization in self._changed_parametrizations
                if _is_same_parametrization(annotation, parametrization)
            ),
            annotation,
        )

    def _get_redefined_field_names(self, model: type[BaseModel]) -> tuple[str, ...]:
        if not self._changed_parametrizations or not lenient_issubclass(model, BaseModel):
            return ()
        if model not in self._redefined_field_names:
            redefined_field_names = tuple(
                name
                for name, parametrizations in self._get_parametrizations_built_by_pydantic(model).items()
                if any(self._reaches_changed_parametrization(parametrization) for parametrization in parametrizations)
            )
            if redefined_field_names and self._is_in_cycle_with_redefined_fields(model):
                redefined_field_names = ()
            self._redefined_field_names[model] = redefined_field_names
        return self._redefined_field_names[model]

    def _get_parametrizations_built_by_pydantic(
        self, model: type[Union[BaseModel, Enum]]
    ) -> dict[str, frozenset[type[BaseModel]]]:
        """Return the parametrizations of generics that pydantic builds itself in the inherited fields of the model.

        They are the ones in the fields of a parametrization (e.g. `Pair[str, int]` in `Box[str]`) that are neither
        in the fields of its generic (e.g. `Pair[T, int]` in `Box`) nor in its type args, which the copy gets from
        the generator already.
        """
        if model in self._parametrizations_built_by_pydantic:
            return self._parametrizations_built_by_pydantic[model]
        parametrizations: dict[str, frozenset[type[BaseModel]]] = {}
        # E.g. enums, which the cycles can have
        if not lenient_issubclass(model, BaseModel):
            return parametrizations
        if not self._can_redefine_fields(model):
            return parametrizations
        origin = cast("type[BaseModel]", model.__pydantic_generic_metadata__["origin"])
        fields = _get_rebuilt_model_fields(model)
        origin_fields = _get_rebuilt_model_fields(origin)
        references = self._get_head_references(model)
        if fields is not None and origin_fields is not None and references.is_complete:
            # A parametrization inherits all of its fields from its generic
            for name, field in fields.items():
                models = _get_models_in_field(field)
                origin_models = _get_models_in_field(origin_fields[name])
                if models is None or origin_models is None:
                    # E.g. a recursive type alias: we cannot know what copying the field would request so the copy
                    # inherits it as on main
                    continue
                field_parametrizations = frozenset(
                    referenced_model
                    for referenced_model in models - origin_models - references.arg_models
                    if lenient_issubclass(referenced_model, BaseModel) and _is_parametrization(referenced_model)
                )
                if field_parametrizations:
                    parametrizations[name] = field_parametrizations
        self._parametrizations_built_by_pydantic[model] = parametrizations
        return parametrizations

    def _can_redefine_fields(self, model: type[BaseModel]) -> bool:
        return _is_parametrization(model) and not any(
            cls in self._changed_models for cls in model.__mro__ if lenient_issubclass(cls, BaseModel)
        )

    def _reaches_changed_parametrization(self, model: type[BaseModel]) -> bool:
        """Whether the model has instructions or leads to such parametrizations through the ones pydantic builds"""
        if model not in self._reaches_changed_parametrizations:
            models_to_visit = [model]
            visited_models: set[type[BaseModel]] = set()
            answer = False
            while models_to_visit:
                current_model = models_to_visit.pop()
                if current_model in visited_models:
                    continue
                visited_models.add(current_model)
                if self._get_changed_parametrization(current_model) in self._changed_parametrizations:
                    answer = True
                    break
                for parametrizations in self._get_parametrizations_built_by_pydantic(current_model).values():
                    models_to_visit.extend(parametrizations)
            self._reaches_changed_parametrizations[model] = answer
        return self._reaches_changed_parametrizations[model]

    def _is_in_cycle_with_redefined_fields(self, model: type[BaseModel]) -> bool:
        """Whether copying the model would request the model itself again if every candidate field was redefined.

        The fields are only redefined in the models outside of such cycles so the cycles stay the same as on main.
        """

        def get_successors(current_model: type[Union[BaseModel, Enum]]) -> "set[type[Union[BaseModel, Enum]]]":
            successors: set[type[Union[BaseModel, Enum]]] = set(self._get_requested_models(current_model))
            for parametrizations in self._get_parametrizations_built_by_pydantic(current_model).values():
                successors.update(
                    parametrization
                    for parametrization in parametrizations
                    if self._reaches_changed_parametrization(parametrization)
                )
            return successors

        if model not in self._models_in_cycles_with_redefined_fields:
            _find_strongly_connected_components(
                model, get_successors, self._models_in_cycles_with_redefined_fields, lambda _, is_cycle: is_cycle
            )
        return self._models_in_cycles_with_redefined_fields[model]

    def _get_head_references(self, model: type[BaseModel]) -> "_ModelReferences":
        if model in self._head_references:
            return self._head_references[model]
        references = _get_head_model_references(model)
        # The references that are not complete can change once the missing names get defined
        if references.is_complete:
            self._head_references[model] = references
        return references

    def _get_requested_models(self, model: type[Union[BaseModel, Enum]]) -> frozenset[type[Union[BaseModel, Enum]]]:
        if model not in self._requested_models:
            if lenient_issubclass(model, BaseModel):
                references = self._get_head_references(model)
                self._requested_models[model] = references.models | references.base_models
            else:
                self._requested_models[model] = frozenset()
        return self._requested_models[model]


class _VersioningQuestion(NamedTuple):
    """A question about a model that SchemaGenerator._is_versioned answers"""

    model: type[Union[BaseModel, Enum]]
    # If False: whether the copy of the model would differ from the model.
    # If True: whether the copy of the model or of any of its subclasses would differ from them.
    includes_subclasses: bool


@final
class SchemaGenerator:
    __slots__ = (
        "_adds_references",
        "_changed_models",
        "_copied_cycles",
        "_cycles_behind_latest_generic_args",
        "_cycles_being_copied",
        "_cycles_to_copy",
        "_generation_analyses",
        "_has_copied_eager_cycles",
        "_instructions_that_differ_from_head",
        "_lock",
        "_model_references",
        "_models_copied_eagerly",
        "_reference_cycles",
        "_root_models",
        "_route_versioning_thread",
        "_versioning_answers",
        "_versions_of_generic_args",
        "annotation_transformer",
        "concrete_models",
        "model_bundle",
    )

    def __init__(
        self,
        model_bundle: _ModelBundle,
        changed_models: "frozenset[type[Union[BaseModel, Enum]]]",
        reference_cycles: "_ReferenceCycles",
        instructions_that_differ_from_head: "collections.abc.Mapping[type[Union[BaseModel, Enum]], tuple[Any, ...]]",
    ) -> None:
        self.annotation_transformer = _AnnotationTransformer(self)
        self.model_bundle = model_bundle
        self._changed_models = changed_models
        # The schema and enum instructions of each model in HeadVersion and in the versions that are newer than this one
        self._instructions_that_differ_from_head = instructions_that_differ_from_head
        self._model_references: dict[type[Union[BaseModel, Enum]], _ModelReferences] = {}
        self._versioning_answers: dict[_VersioningQuestion, bool] = {}
        self._reference_cycles = reference_cycles
        self._generation_analyses: dict[type[Union[BaseModel, Enum]], _GenerationAnalysis] = {}
        self._versions_of_generic_args: dict[type[BaseModel], bool] = {}
        # The reference cycles among the models that this version copies, or None for the models outside of them
        self._cycles_to_copy: dict[type[BaseModel], Union[frozenset[type[BaseModel]], None]] = {}
        # Whether the instructions of this version add references to models (see _adds_references_to_models)
        self._adds_references: Union[bool, None] = None
        # The reference cycles whose copies we are generating right now, innermost last
        self._cycles_being_copied: list[frozenset[type[BaseModel]]] = []
        # The models that this version copies in any case: the ones with instructions, which the generator copies
        # when it is created, and the ones that the routes use (see _add_route_models). Along with all of the models
        # that they refer to, they are the models copied eagerly (see _is_copied_eagerly)
        self._root_models: set[type[BaseModel]] = set(self.model_bundle.schemas)
        self._models_copied_eagerly: Union[set[type[BaseModel]], None] = None
        # The reference cycles that the generator has copied
        self._copied_cycles: set[frozenset[type[BaseModel]]] = set()
        # Whether the generator has copied a cycle that it copies eagerly (see _is_copied_eagerly)
        self._has_copied_eager_cycles = False
        # The thread that versions the routes of the version right now, if any (see versioning_routes)
        self._route_versioning_thread: Union[int, None] = None
        # The reference cycles that some generics keep the head models of in their type args
        self._cycles_behind_latest_generic_args: set[frozenset[type[BaseModel]]] = set()
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
            if not self._needs_copy(model):
                return model
            if model not in self.concrete_models:
                for cycle in self._cycles_being_copied:
                    if model in cycle:
                        raise CadwynError(
                            f"{model.__qualname__} was requested from outside of its reference cycle while Cadwyn "
                            "was copying the cycle. This is a bug in Cadwyn, or a hook of the model "
                            "(e.g. __init_subclass__) that asks Cadwyn for its versioned copies."
                        )
                cycle = self._get_cycle_to_copy(model)
                if cycle is None:
                    wrapper = self._get_wrapper_for_model(model)
                    model_copy = wrapper.generate_model_copy(self)
                    if self._copied_cycles and lenient_issubclass(model_copy, BaseModel):
                        self._check_copy_outside_of_cycles(cast("type[BaseModel]", model), model_copy)
                    self.concrete_models[model] = model_copy
                else:
                    self._copy_cycle(cycle)
            return cast("type[_T_ANY_MODEL]", self.concrete_models[model])

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        return self[base]

    def dependency_differs_from_head(self, dependency: Callable[..., Any]) -> bool:
        """Whether this version's copy of the dependency could validate requests differently from the dependency.

        That is, whether the dependency's parameters validate models or enums that this version changes, directly,
        through sub-dependencies, or through the types of the fields of models. Unlike the references of models
        (see _get_references), this only follows what pydantic validates: the resolved fields of each model, after
        its subclasses override them, so not the fields of bases that a subclass overrides, class variables,
        field attributes such as default factories, or the type args of generics and generic type aliases that no
        field uses. The typed extra fields (`__pydantic_extra__`) of models that allow extra fields count as fields.
        A model also differs if its bases have instructions, because its copy inherits from their copies, unless
        the instructions only change the fields that the model overrides.
        """
        if not self._instructions_that_differ_from_head:
            return False
        collector = _ReferencedModelCollector(expands_type_aliases=True)
        collector.annotation_transformer.record_dependency(dependency)
        models_to_visit = [*collector.models, *collector.arg_models]
        visited_models: set[type[Union[BaseModel, Enum]]] = set()
        while models_to_visit:
            model = models_to_visit.pop()
            if model in visited_models:
                continue
            visited_models.add(model)
            if model in self._instructions_that_differ_from_head:
                return True
            if lenient_issubclass(model, BaseModel):
                if any(self._base_differs_from_head(model, base_index) for base_index in range(1, len(model.__mro__))):
                    return True
                models_to_visit.extend(_get_models_validated_by_fields(model))
        return False

    def _base_differs_from_head(self, model: type[BaseModel], base_index: int) -> bool:
        instructions = self._instructions_that_differ_from_head.get(model.__mro__[base_index])
        if not instructions:
            return False
        # The fields that the model or the bases between them override keep their own definitions in the copy
        overridden_names = {
            name for cls in model.__mro__[:base_index] for name in _get_annotations_without_evaluating(cls)
        }
        return any(not _only_changes_fields(instruction, overridden_names) for instruction in instructions)

    def _needs_copy(self, model: type[Union[BaseModel, Enum]]) -> bool:
        """Whether this version uses a copy of the model instead of the head model.

        Just like Cadwyn always did, we copy every model for every version except for the ones whose copies would
        be the same as the head models (see _is_versioned) and that we cannot or should not copy:
        * The enums, whose members must stay the same objects (e.g. strict models reject the members of a copy
          of their enum)
        * The models in reference cycles (e.g. `children: "list[Category]"`), whose copying would never finish.
          The copies of the models that refer to them use the head models too.
        We copy all other models even if their copies are the same as the head models because their copies
        differ from them in subtle ways that the older versions rely on, e.g. their OpenAPI names and their
        dunder methods such as __get_pydantic_json_schema__ that the copies do not have.
        The decisions are cached so the models that are defined after the generator has decided about the models
        that they refer to or inherit from do not change these decisions.
        """
        if self._cycles_to_copy.get(model) is not None:
            # We decided to copy its cycle so we keep that decision even if the subclasses that it depended on
            # change later (see _is_versioned)
            return True
        # Finding the cycles is much cheaper than looking at all of the subclasses in _is_versioned
        if lenient_issubclass(model, BaseModel) and not self._reference_cycles.is_in_cycle(model):
            return True
        return self._is_versioned(model)

    def _is_versioned(self, model: type[Union[BaseModel, Enum]]) -> bool:
        """Whether the copy of the model or of any of its subclasses could differ from them in some version.

        The copy of a model differs from it if the model has schema or enum instructions in any version, or if
        its bases differ, or if any of the models in its fields, annotations, generic args, etc. are versioned.
        The subclasses matter for two reasons. The copies of the subclasses must not inherit from the head model
        (e.g. its __init_subclass__ or __init__). And a model accepts the instances of its subclasses, so the head
        model would accept the head instances of a versioned subclass (e.g. with SerializeAsAny) while its copy
        rejects them. The copy of a model only differs from it because of its bases if the bases themselves
        differ, not if they only have subclasses that differ.
        If we do not know all of the models that the model refers to (see _ModelReferences.is_complete),
        we assume that it is versioned.
        The parametrizations of generics are not subclasses here (see _get_subclasses_that_can_be_versioned).
        The decision only depends on the references between the models and on the subclasses that exist when
        the generator first needs the decision, not on the order of generation. We do not keep a decision that
        depended on the subclasses that were still being created.
        """
        if not self._changed_models:
            # Nothing has instructions so we do not need to look into the models
            return False
        return self._reaches_changed_models(_VersioningQuestion(model, includes_subclasses=True))

    def _reaches_changed_models(self, question: "_VersioningQuestion") -> bool:
        if question in self._versioning_answers:
            return self._versioning_answers[question]
        visited_questions: set[_VersioningQuestion] = set()
        questions_to_visit = [question]
        answer = False
        depends_on_unknown_subclasses = False
        while questions_to_visit:
            current_question = questions_to_visit.pop()
            if current_question in visited_questions or self._versioning_answers.get(current_question) is False:
                continue
            visited_questions.add(current_question)
            if self._versioning_answers.get(current_question) or self._is_changed(current_question):
                answer = True
                break
            dependent_questions, are_all_known = self._get_dependent_questions(current_question)
            depends_on_unknown_subclasses |= not are_all_known
            questions_to_visit.extend(dependent_questions)
        if answer:
            self._versioning_answers[question] = True
        elif not depends_on_unknown_subclasses:
            # None of the visited questions lead to changed models either
            self._versioning_answers.update(dict.fromkeys(visited_questions, False))
        # Otherwise, we ask again next time because the subclasses that are still being created could change it
        return answer

    def _is_changed(self, question: "_VersioningQuestion") -> bool:
        if question.includes_subclasses:
            return False
        return question.model in self._changed_models or not self._get_references(question.model).is_complete

    def _get_dependent_questions(self, question: "_VersioningQuestion") -> "tuple[list[_VersioningQuestion], bool]":
        """Return the questions that the answer depends on, and whether we know all of them"""
        model = question.model
        if question.includes_subclasses:
            subclasses, are_all_known = _get_subclasses_that_can_be_versioned(model)
            return [
                _VersioningQuestion(model, includes_subclasses=False),
                *(_VersioningQuestion(_unwrap_model(subclass), includes_subclasses=True) for subclass in subclasses),
            ], are_all_known
        references = self._get_references(model)
        return [
            *(
                _VersioningQuestion(referenced_model, includes_subclasses=True)
                for referenced_model in references.models | references.arg_models
            ),
            *(_VersioningQuestion(base, includes_subclasses=False) for base in references.base_models),
        ], True

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        return _change_version_of_generic_metadata(self, self, model)

    def _should_version_generic_args(
        self, generic: type[BaseModel], generation_stack: list[type[Union[BaseModel, Enum]]]
    ) -> bool:
        """Whether the copy of the generic should use the copies of its type args instead of the head ones.

        We only version the args when they refer to models with schema or enum instructions so that all other
        generics stay exactly as they are. We also keep the head args when their copies could not be generated:
        when the args refer back to the generic (e.g. `children: Page["Category"]` within `Category`) or when
        generating their copies would recurse forever (e.g. `children: "list[Tree]"` within a versioned `Tree`).
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
        if not self._needs_copy(model):
            # We use the head model so nothing gets generated, and it does not refer to models with instructions
            return _GenerationAnalysis(terminates=True, refers_to_changed_models=False)
        cycle = self._get_cycle_to_copy(model)
        if cycle is not None:
            return self._analyze_generation_of_cycle(cycle)
        if model in self._generation_analyses:
            return self._generation_analyses[model]
        if model in generation_stack:  # pragma: no cover # The cycles of copies are analyzed above
            # The models that generating this model requests lead back to it so its generation would never finish
            return _GenerationAnalysis(terminates=False, refers_to_changed_models=False)
        generation_stack.append(model)
        references = self._get_references(model)
        requested_models = set(references.models | references.base_models)
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

    def _analyze_generation_of_cycle(self, cycle: frozenset[type[BaseModel]]) -> _GenerationAnalysis:
        """Analyze the generation of the copies of a reference cycle that this version copies when they are needed.

        If this version copies the cycle in any case (see _is_copied_eagerly), where main failed, the generic args
        that refer to it use its copies like any other versioned args. Otherwise, they keep the head models, exactly
        as on main, which never copied such a cycle. Copying such a cycle later on its own (e.g. in
        migrate_response_body, where main failed) does not change them.
        """
        if self._is_copied_eagerly(cycle):
            # Its models need copies so they are versioned
            return _GenerationAnalysis(terminates=True, refers_to_changed_models=True)
        self._cycles_behind_latest_generic_args.add(cycle)
        return _GenerationAnalysis(terminates=False, refers_to_changed_models=False)

    @contextlib.contextmanager
    def versioning_routes(
        self, route_models: "collections.abc.Iterable[type[Union[BaseModel, Enum]]]"
    ) -> Generator[None, None, None]:
        """Register the models of the routes of the version while its routes are versioned (see _add_route_models)"""
        self._add_route_models(route_models)
        with self._lock:
            self._route_versioning_thread = threading.get_ident()
        try:
            yield
        finally:
            with self._lock:
                self._route_versioning_thread = None

    def _add_route_models(self, models: "collections.abc.Iterable[type[Union[BaseModel, Enum]]]") -> None:
        """Make the generator copy the cycles that the models of the routes refer to eagerly (see _is_copied_eagerly).

        Route generation registers all models of a version's routes before it versions any of them, so the
        decisions do not depend on the order of the routes.
        """
        with self._lock:
            route_models = {
                model for model in models if lenient_issubclass(model, BaseModel) and _is_versionable_model(model)
            }
            if route_models <= self._root_models:
                return
            # The models are only registered after the check so the next attempt to start the app fails the same way.
            # There are no cycles behind the latest generic args before the models copied eagerly are known because
            # finding such cycles needs them (see _analyze_generation_of_cycle)
            if self._models_copied_eagerly is not None:
                models_copied_eagerly = self._get_models_copied_eagerly(route_models, self._models_copied_eagerly)
                for cycle in self._cycles_behind_latest_generic_args:
                    if not cycle.isdisjoint(models_copied_eagerly):
                        # E.g. a model with instructions has `Page[Category]`, which kept the latest Category when the
                        # generator was created, and a route uses Category directly
                        raise _get_latest_generic_args_error(cycle)
                self._models_copied_eagerly = models_copied_eagerly
            self._root_models |= route_models

    def _is_copied_eagerly(self, cycle: frozenset[type[BaseModel]]) -> bool:
        """Whether this version copies the cycle in any case, where main failed.

        This version copies the models with instructions when its generator is created, the models of its routes,
        and every model that they refer to (except for the generic args that keep the head models). The decision
        only depends on the references between the models and on the models of the routes, which route generation
        registers before it versions them, so it does not depend on the order in which the models are requested.
        """
        if self._models_copied_eagerly is None:
            self._models_copied_eagerly = self._get_models_copied_eagerly(self._root_models)
        return not cycle.isdisjoint(self._models_copied_eagerly)

    def _get_models_copied_eagerly(
        self,
        root_models: "collections.abc.Iterable[type[BaseModel]]",
        models_copied_eagerly: "collections.abc.Set[type[BaseModel]]" = frozenset(),
    ) -> set[type[BaseModel]]:
        """Return the models copied eagerly along with the given ones: they and every model that they refer to"""
        models_to_visit = [model for model in root_models if self._needs_copy(model)]
        return self._get_models_reached_by_copying(models_to_visit, models_copied_eagerly)

    def _get_models_reached_by_copying(
        self,
        models_to_visit: list[type[BaseModel]],
        reached_models: "collections.abc.Set[type[BaseModel]]" = frozenset(),
    ) -> set[type[BaseModel]]:
        """Return the reached models, the models to visit, and every model whose copy copying them generates"""
        models = set(reached_models)
        while models_to_visit:
            model = models_to_visit.pop()
            if model not in models:
                models.add(model)
                models_to_visit.extend(self._get_models_that_copying_requests(model))
        return models

    def _is_in_cycle_used_by(
        self, cls: type, get_models_reached_by_copying: Callable[[], set[type[BaseModel]]]
    ) -> bool:
        """Whether the head model of the class (e.g. of a copy) is in a cycle whose copies the model uses"""
        cls = _unwrap_model(cast("type[BaseModel]", cls))
        if not _is_versionable_model(cls) or not lenient_issubclass(cls, BaseModel) or not self._needs_copy(cls):
            return False
        cycle = self._get_cycle_to_copy(cls)
        return cycle is not None and (
            self._is_copied_eagerly(cycle) or not cycle.isdisjoint(get_models_reached_by_copying())
        )

    def _check_copy_outside_of_cycles(self, model: type[BaseModel], model_copy: type[BaseModel]) -> None:
        """Check a copy outside of the copied cycles if it can use the head models of these cycles.

        Once a version copies a cycle eagerly, where main failed, we check every copy of the version. Otherwise,
        we only check the copies that reach the lazily copied cycles, so the copies that work as on main stay so.
        """
        get_models_reached_by_copying = functools.cache(
            lambda: self._get_models_reached_by_copying(list(self._get_models_that_copying_requests(model)))
        )
        if self._has_copied_eager_cycles or any(
            not cycle.isdisjoint(get_models_reached_by_copying()) for cycle in self._copied_cycles
        ):
            is_in_cycle_used_by = functools.partial(
                self._is_in_cycle_used_by, get_models_reached_by_copying=get_models_reached_by_copying
            )
            self._check_copy(
                model_copy,
                # The head classes in the cycles whose copies this version uses instead of them where the model is
                # used: the cycles that this version copies eagerly, and the ones that copying the model itself copies
                # (e.g. `category: Category` along with `related: Page[Category]`, whose args are the head models
                # when Category is copied lazily). Other cycles do not count because the generics around them keep
                # their head models, as on main.
                lambda cls: not hasattr(cls, "__cadwyn_original_model__") and is_in_cycle_used_by(cls),
                is_in_cycle_used_by,
                skipped_classes=set(self.concrete_models.values()),
            )

    def _check_copy(
        self,
        model_copy: type[BaseModel],
        is_latest_model: Callable[[type], bool],
        is_checked_field_model: Callable[[type], bool],
        *,
        is_latest_cycle_model: Union[Callable[[type], bool], None] = None,
        skipped_classes: "collections.abc.Set[type]" = frozenset(),
    ) -> None:
        """Raise if the copy validates, serializes, or defaults to head models that it should not use.

        Cadwyn only versions what it can see, so the copies would silently keep the head models wherever pydantic
        finds them on its own, e.g. in the return types of computed fields and serializers, in quoted names within
        other annotations (`list["Model"]`), or in generics whose args stay the head models. We look at what
        pydantic has actually built so we do not have to know every such place. We do not look into the schemas of
        skipped_classes, which are checked on their own. We check the defaults of the fields whose schemas use
        the classes for which is_checked_field_model is true, and the default values of all other fields for the
        head models for which is_latest_cycle_model (is_latest_model by default) is true.
        """
        if not model_copy.__pydantic_complete__:
            # E.g. defer_build=True. We only check the copies in the versions that copy cycles, where main failed
            model_copy.model_rebuild(raise_errors=False, _types_namespace={})
            if not model_copy.__pydantic_complete__:
                raise SchemaGenerationError(
                    f"Cadwyn cannot check the versioned copy of {model_copy.__qualname__} for the latest versions of "
                    "the models that refer to each other because pydantic cannot build it yet."
                )
        core_schema = model_copy.__pydantic_core_schema__
        skipped_classes = set(skipped_classes) - {model_copy}
        latest_models = [
            cls
            for cls in _get_classes_in_core_schema(core_schema, skipped_classes=skipped_classes)
            if is_latest_model(cls)
        ]
        if latest_models:
            raise SchemaGenerationError(
                f"Cadwyn cannot generate the versioned copy of {model_copy.__qualname__} because it would use the "
                f"latest {_get_cycle_name(latest_models)}, which need versioning, where pydantic resolves them on its "
                "own: e.g. in the return types of computed fields or serializers, in names quoted within other "
                'annotations (`list["Model"]`, which you can write as `"list[Model]"` instead), or in the type args '
                "of generics that keep the latest models."
            )
        self._check_defaults(model_copy, core_schema, is_checked_field_model, is_latest_cycle_model or is_latest_model)

    def _check_defaults(
        self,
        model_copy: type[BaseModel],
        core_schema: Any,
        is_checked_field_model: Callable[[type], bool],
        is_latest_cycle_model: Callable[[type], bool],
    ) -> None:
        """Raise if the default of a field that uses the checked models could be a head model that needs versioning.

        Cadwyn does not version default values, so e.g. `settings: Settings = Settings()` would keep the head
        Settings and lose the changes of the older versions. We look into the default values themselves, but we
        cannot know what most default factories return, so we only accept the ones that create the copies.
        Enum members do not count because they are serialized by their values, as in all other models. The fields
        that do not use the checked models (e.g. `Any` or `dict`) keep their defaults as on main, except for the
        head models of the cycles in their values.
        """
        definitions = _get_core_schema_definitions(core_schema)
        field_schemas = _get_field_core_schemas(model_copy, core_schema, definitions)
        for name, field in model_copy.model_fields.items():
            field_schema = None if field_schemas is None else field_schemas.get(name)
            uses_checked_models = field_schema is None or any(
                is_checked_field_model(cls)
                for cls in _get_classes_in_core_schema(field_schema, definitions=definitions)
            )
            is_latest_value_model = self._is_latest_versioned_model if uses_checked_models else is_latest_cycle_model
            latest_values = [
                value
                for value in _iter_values_within(field.default)
                if isinstance(value, BaseModel) and is_latest_value_model(type(value))
            ]
            if latest_values:
                raise SchemaGenerationError(
                    f"Cadwyn cannot generate the versioned copy of {model_copy.__qualname__} because the default of "
                    f"the field '{name}' of {model_copy.__qualname__} contains an instance of the latest "
                    f"{type(latest_values[0]).__qualname__}, which needs versioning. Please use a default factory "
                    "instead, e.g. the class of the field's model itself (`Field(default_factory=Model)`)."
                )
            if (
                uses_checked_models
                and field.default_factory is not None
                and not self._is_safe_default_factory(field.default_factory)
            ):
                raise SchemaGenerationError(
                    f"Cadwyn cannot verify what the default factory of the field '{name}' of "
                    f"{model_copy.__qualname__} returns, so its older versions could get the latest versions of the "
                    "models that need versioning. Please use `default_factory=list` or `default_factory=dict`, or "
                    "the class of the field's model itself (`Field(default_factory=Model)`)."
                )

    def _is_safe_default_factory(self, factory: Any) -> bool:
        """Whether the default factory provably creates the copies of this version rather than the head models"""
        factory = getattr(factory, "_original_callable", factory)
        # Subclasses of partial can override what calling them does
        if type(factory) is functools.partial:
            return self._is_safe_default_factory(factory.func) and not any(
                isinstance(value, BaseModel) and self._is_latest_versioned_model(type(value))
                for value in _iter_values_within([factory.args, factory.keywords])
            )
        if factory in _BUILTIN_CONTAINER_TYPES:
            return True
        if isinstance(factory, type):
            return self._is_class_of_this_version(factory)
        # Only pydantic's own constructors are known to create instances of the class that they are bound to
        return (
            inspect.ismethod(factory)
            and getattr(factory, "__func__", None) in _PYDANTIC_CONSTRUCTORS
            and isinstance(factory.__self__, type)
            and self._is_class_of_this_version(factory.__self__)
            # E.g. RootModel.model_construct calls the model_construct of the next class in the MRO
            and not any(
                factory.__name__ in vars(cls) for cls in factory.__self__.__mro__ if not _is_pydantic_model_class(cls)
            )
        )

    def _is_class_of_this_version(self, cls: type) -> bool:
        """Whether the class is a copy or a head model that this version uses as it is"""
        return hasattr(cls, "__cadwyn_original_model__") or (
            _is_versionable_model(cls) and not self._is_versioned(cast("type[BaseModel]", cls))
        )

    def _is_versioned_class(self, cls: type) -> bool:
        """Whether the class is a head model that needs versioning or a copy of one"""
        return _is_versionable_model(cls) and self._is_versioned(_unwrap_model(cast("type[BaseModel]", cls)))

    def _is_latest_versioned_model(self, cls: type) -> bool:
        """Whether the class is a head model that needs versioning"""
        return (
            _is_versionable_model(cls)
            and not hasattr(cls, "__cadwyn_original_model__")
            and self._is_versioned(cast("type[BaseModel]", cls))
        )

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
                models_to_visit.extend(references.models | references.arg_models | references.base_models)
        return False

    def _get_cycle_to_copy(self, model: type[Union[BaseModel, Enum]]) -> Union[frozenset[type[BaseModel]], None]:
        """Return the reference cycle among the models that this version copies that the model is in, if any.

        Copying a model requests the copies of the models that it refers to, so copying a model in such a cycle
        on its own would never finish. We copy all models of the cycle at once instead (see _copy_cycle).
        The cycles are found statically in the references between the models that this version copies, which
        can differ from the head references because of instructions. Without instructions that add references
        to models, every such cycle is within a cycle among the head models, so we only look for cycles around
        the models in the head cycles, which keeps all other models exactly as on main.
        """
        if not lenient_issubclass(model, BaseModel):
            return None
        if model not in self._cycles_to_copy:
            if not self._reference_cycles.is_in_cycle(model) and not self._adds_references_to_models():
                self._cycles_to_copy[model] = None
            else:
                _find_strongly_connected_components(
                    model,
                    self._get_models_that_copying_requests,
                    self._cycles_to_copy,
                    lambda component, is_cycle: frozenset(component) if is_cycle else None,
                )
        return self._cycles_to_copy[model]

    def _get_models_that_copying_requests(self, model: type[BaseModel]) -> frozenset[type[BaseModel]]:
        """Return the models whose copies copying the model would generate.

        The models that this version does not copy end the chain of copying because the head models are used.
        """
        references = self._get_references(model)
        return frozenset(
            referenced_model
            for referenced_model in references.models | references.base_models
            if lenient_issubclass(referenced_model, BaseModel) and self._needs_copy(referenced_model)
        )

    def _adds_references_to_models(self) -> bool:
        """Whether the instructions of this version make any model refer to models that its head model does not.

        E.g. `schema(Category).field("parent").existed_as(type=Optional[Category])`. Only such instructions can
        create reference cycles that do not exist among the head models.
        """
        if self._adds_references is None:
            self._adds_references = False
            for model in self._changed_models:
                if lenient_issubclass(model, BaseModel) and model in self.model_bundle.schemas:
                    references = self._get_references(model)
                    head_references = self._reference_cycles.get_requested_models(model)
                    if not (references.models | references.base_models) <= head_references:
                        self._adds_references = True
                        break
        return self._adds_references

    def _copy_cycle(self, cycle: frozenset[type[BaseModel]]) -> None:
        """Generate the copies of all models of a reference cycle at once.

        Copying a model in the cycle refers to the other models of the cycle, and to itself, through forward refs
        in its annotations. Pydantic resolves them once all of the copies exist (see _CycleCopier).
        The copies are only published once all of them are complete and every check passed, so no other request
        can see them before, and a failed check leaves nothing behind: the next attempt fails the same way.
        """
        if (
            self._route_versioning_thread == threading.get_ident() and cycle in self._cycles_behind_latest_generic_args
        ):  # pragma: no cover # Recording the routes finds every model that versioning them requests (see conftest)
            raise _get_latest_generic_args_error(cycle)
        creation_order = self._get_creation_order_of_cycle(cycle)
        self._cycles_being_copied.append(cycle)
        try:
            copies = _CycleCopier(self, cycle).copy(creation_order)
            for model in creation_order:
                # The defaults of the models in the cycle are checked for every model that needs versioning
                self._check_copy(
                    copies[model],
                    self._is_latest_versioned_model,
                    self._is_versioned_class,
                    is_latest_cycle_model=cycle.__contains__,
                )
        finally:
            self._cycles_being_copied.pop()
        is_copied_eagerly = self._is_copied_eagerly(cycle)
        if is_copied_eagerly:
            # The copies generated before could also use the head models of this cycle. They cannot reach the cycles
            # that are copied lazily because copying them would have copied these cycles first
            copied_classes = {*self.concrete_models.values(), *copies.values()}
            earlier_model_copies = [
                model_copy for model_copy in self.concrete_models.values() if lenient_issubclass(model_copy, BaseModel)
            ]
            for model_copy in earlier_model_copies:
                self._check_copy(
                    model_copy,
                    cycle.__contains__,
                    lambda cls: _unwrap_model(cast("type[BaseModel]", cls)) in cycle,
                    skipped_classes=copied_classes,
                )
        for model, model_copy in copies.items():
            self.concrete_models[model] = model_copy
        self._copied_cycles.add(cycle)
        if is_copied_eagerly:
            self._has_copied_eager_cycles = True

    def _get_creation_order_of_cycle(self, cycle: frozenset[type[BaseModel]]) -> list[type[BaseModel]]:
        """Return the order in which to create the copies of the models in the cycle, or raise if we cannot copy it.

        A copy must be created after the copies of the models in the cycle that it needs as classes, e.g. its bases
        or its default factories. Only its annotations can refer to the others through forward refs.
        """
        cycle_name = _get_cycle_name(cycle)
        models_needed_as_classes: dict[type[BaseModel], set[type[Union[BaseModel, Enum]]]] = {}
        for model in sorted(cycle, key=_get_sort_key_of_model):
            references = self._get_references(model)
            if not references.is_complete:
                raise SchemaGenerationError(
                    f"Cadwyn cannot generate the versioned copies of {cycle_name} because they refer to each other "
                    f"and the annotations of {model.__qualname__} or of the functions that it uses refer to names "
                    "that are not defined yet or to type aliases that refer to themselves."
                )
            if (
                references.arg_models
                and self._should_version_generic_args(model, generation_stack=[])
                and references.arg_models & cycle
            ):  # pragma: no cover # _should_version_generic_args never versions args that lead back to the cycle
                raise CadwynError(f"The generic args of {model.__qualname__} lead back to its reference cycle")
            models_needed_as_classes[model] = set((references.hard_models | references.base_models) & cycle)

        creation_order = []
        while models_needed_as_classes:
            models_to_create = [model for model, needed in models_needed_as_classes.items() if not needed]
            if not models_to_create:
                raise SchemaGenerationError(
                    f"Cadwyn cannot generate the versioned copies of {cycle_name} because they refer to each other "
                    "in positions that need the classes themselves rather than forward refs to them, e.g. in "
                    "default factories, in the metadata of Annotated, in type aliases, or in the args of generics "
                    "that are not from typing or collections.abc. Please refer to these models in the annotations "
                    "of fields instead."
                )
            for model in models_to_create:
                del models_needed_as_classes[model]
                for needed in models_needed_as_classes.values():
                    needed.discard(model)
            creation_order.extend(models_to_create)
        return creation_order

    def _is_copied_with_its_cycle(self, model: type[BaseModel]) -> bool:
        return self._needs_copy(model) and self._get_cycle_to_copy(model) is not None

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
                references = _ModelReferences(frozenset(), frozenset(), frozenset())
            elif model in self.model_bundle.schemas:
                references = self.model_bundle.schemas[model].get_references(self.model_bundle.schemas)
            else:
                # We do not wrap the model because wrapping copies parts of it and we may never need its copy
                references = self._reference_cycles.get_head_references(model)
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
            wrapper = _wrap_pydantic_model(model, self._reference_cycles.get_redefined_fields(model))
            self.model_bundle.schemas[model] = wrapper
        elif issubclass(model, Enum):
            if model in self.model_bundle.enums:
                return self.model_bundle.enums[model]
            wrapper = _EnumWrapper(model)
            self.model_bundle.enums[model] = wrapper
        else:
            assert_never(model)
        return wrapper


def _get_cycle_name(cycle: "collections.abc.Iterable[type]") -> str:
    return ", ".join(sorted(model.__qualname__ for model in cycle))


def _get_latest_generic_args_error(cycle: frozenset[type[BaseModel]]) -> SchemaGenerationError:
    return SchemaGenerationError(
        f"Cadwyn cannot generate the versioned copies of {_get_cycle_name(cycle)} because they refer to each other "
        "and the same version also keeps the latest models in the type args of generics that refer to them, e.g. "
        "in `Page[Model]` within a model with instructions that refers to them only through such generics. This is "
        "a known limitation: Cadwyn decides about the generics of the models with instructions before it knows the "
        "routes, and a route that uses these models directly needs their own copies. Until it is lifted, use these "
        "models in the routes only through such generics."
    )


# The keys of the core schemas whose values are not schemas, such as default values, which can be anything
_CORE_SCHEMA_KEYS_WITHOUT_SCHEMAS = frozenset({"default", "default_factory", "expected", "metadata"})


def get_models_that_routes_request(routers: "collections.abc.Iterable[fastapi.routing.APIRouter]") -> set[type[Any]]:
    """Return the models that versioning the routes of the routers requests from a generator directly"""
    collector = _ReferencedModelCollector()
    for router in routers:
        for route in router.routes:
            if isinstance(route, fastapi.routing.APIRoute):
                collector.annotation_transformer.record_route(route)
    return collector.models


def _iter_core_schema_nodes(
    core_schema: Any,
    *,
    definitions: Union[dict[str, Any], None] = None,
    should_skip_children: Union[Callable[[dict[str, Any]], bool], None] = None,
) -> "collections.abc.Iterator[dict[str, Any]]":
    """Iterate over the schemas within the core schema, following its definition refs if definitions are given"""
    nodes_to_visit = [core_schema]
    visited_ids: set[int] = set()
    while nodes_to_visit:
        node = nodes_to_visit.pop()
        if id(node) in visited_ids:
            continue
        visited_ids.add(id(node))
        if isinstance(node, dict):
            yield node
            if should_skip_children is not None and should_skip_children(node):
                continue
            if definitions is not None and node.get("type") == "definition-ref":
                nodes_to_visit.append(definitions.get(node.get("schema_ref")))
            nodes_to_visit.extend(value for key, value in node.items() if key not in _CORE_SCHEMA_KEYS_WITHOUT_SCHEMAS)
        elif isinstance(node, list | tuple):
            nodes_to_visit.extend(node)


def _get_classes_in_core_schema(
    core_schema: Any,
    *,
    definitions: Union[dict[str, Any], None] = None,
    skipped_classes: "collections.abc.Set[type]" = frozenset(),
) -> set[type]:
    """Return the classes that the core schema validates or serializes, e.g. the models in its fields.

    We do not look into the schemas of skipped_classes.
    """

    def should_skip_children(node: dict[str, Any]) -> bool:
        cls = node.get("cls")
        return isinstance(cls, type) and cls in skipped_classes

    return {
        node["cls"]
        for node in _iter_core_schema_nodes(
            core_schema, definitions=definitions, should_skip_children=should_skip_children
        )
        if isinstance(node.get("cls"), type)
    }


def _get_core_schema_definitions(core_schema: Any) -> dict[str, Any]:
    return {node["ref"]: node for node in _iter_core_schema_nodes(core_schema) if isinstance(node.get("ref"), str)}


def _get_field_core_schemas(
    model: type[BaseModel], core_schema: Any, definitions: dict[str, Any]
) -> Union[dict[str, Any], None]:
    """Return the core schemas of the fields of the model, or None if we cannot find them (e.g. for RootModel)"""
    for node in _iter_core_schema_nodes(core_schema, definitions=definitions):
        if node.get("type") == "model" and node.get("cls") is model:
            for inner_node in _iter_core_schema_nodes(
                node.get("schema"), definitions=definitions, should_skip_children=lambda n: n.get("type") == "model"
            ):
                if inner_node.get("type") == "model-fields":
                    return {name: field.get("schema") for name, field in inner_node["fields"].items()}
            return None
    return None  # pragma: no cover # The schema of a model always describes the model itself


# Unique across all versions and all attempts so that the names of different copies never get mixed up, e.g.
# through the caches of typing: Optional[ForwardRef("X")] is cached so its ForwardRef("X") is shared.
_reference_cycle_member_ids = itertools.count()


def _get_sort_key_of_model(model: type[BaseModel]) -> tuple[str, str]:
    return model.__module__, model.__qualname__


def _change_version_of_generic_metadata(
    generator: "Union[SchemaGenerator, _CycleCopier]", schema_generator: "SchemaGenerator", model: type[BaseModel]
) -> PydanticGenericMetadata:
    generic_metadata = model.__pydantic_generic_metadata__
    origin = generic_metadata["origin"]
    if not schema_generator._should_version_generic_args(model, generation_stack=[]):
        if origin is not None and schema_generator._is_copied_with_its_cycle(origin):
            # Pydantic identifies a parametrized generic by its origin and args, so with the head origin, the copy
            # of e.g. TreeNode[int] would be mistaken for the head TreeNode[int] wherever both of them are used
            return {**generic_metadata, "origin": generator.change_version_of_base(origin)}
        return generic_metadata
    # Pydantic resolves the fields of a parametrized generic (such as Page[Item]) through its origin and args
    # so they must point to this version's classes too
    return {
        "origin": generator.change_version_of_base(origin) if origin is not None else None,
        "args": generator.annotation_transformer.change_version_of_annotation(_eval_generic_args(model)),
        "parameters": generic_metadata["parameters"],
    }


@final
class _CycleCopier:
    """Generates the copies of all models in a reference cycle at once (see SchemaGenerator._copy_cycle).

    Copying the models goes through it instead of the generator. In the annotations, the models of the cycle are
    replaced by forward refs to their copies, which pydantic resolves once all of the copies exist. Everywhere else,
    e.g. in bases and default factories, they are replaced by their copies themselves, which already exist because
    of the order of creation. All other models are requested from the generator as usual.
    """

    __slots__ = "_forward_refs", "annotation_transformer", "copies", "model_bundle", "schema_generator"

    def __init__(self, schema_generator: "SchemaGenerator", cycle: frozenset[type[BaseModel]]) -> None:
        self.schema_generator = schema_generator
        self.model_bundle = schema_generator.model_bundle
        self.annotation_transformer = _AnnotationTransformer(self)
        self.copies: dict[type[BaseModel], type[BaseModel]] = {}
        self._forward_refs = {
            model: ForwardRef(f"_CadwynReferenceCycleMember{next(_reference_cycle_member_ids)}") for model in cycle
        }

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> Any:
        if not _is_versionable_model(model) or _unwrap_model(model) not in self._forward_refs:
            return self.schema_generator[model]
        member = cast("type[BaseModel]", _unwrap_model(model))
        if self.annotation_transformer.is_in_hard_position:
            return self._get_copy(member)
        return self._forward_refs[member]

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        if _is_versionable_model(base) and _unwrap_model(base) in self._forward_refs:
            return cast("type[_T_ANY_MODEL]", self._get_copy(_unwrap_model(base)))
        return self.schema_generator.change_version_of_base(base)

    def change_version_of_generic_metadata(self, model: type[BaseModel]) -> PydanticGenericMetadata:
        return _change_version_of_generic_metadata(self, self.schema_generator, model)

    def _get_copy(self, model: Any) -> type[BaseModel]:
        if model not in self.copies:  # pragma: no cover # The order of creation makes sure that it exists
            raise CadwynError(f"The copy of {model.__qualname__} was needed before it was created")
        return self.copies[model]

    def copy(self, creation_order: list[type[BaseModel]]) -> dict[type[BaseModel], type[BaseModel]]:
        for model in creation_order:
            wrapper = self.schema_generator._get_wrapper_for_model(model)
            self.copies[model] = wrapper.generate_model_copy(self)
        copies_by_name = {
            self._forward_refs[model].__forward_arg__: model_copy for model, model_copy in self.copies.items()
        }
        for model_copy in self.copies.values():
            # The parent namespace stays with the copy, unlike the namespace passed to model_rebuild, so pydantic
            # can also resolve the forward refs when it rebuilds the copy or parametrizes it later
            parent_namespace = unpack_lenient_weakvaluedict(model_copy.__pydantic_parent_namespace__) or {}
            model_copy.__pydantic_parent_namespace__ = build_lenient_weakvaluedict(parent_namespace | copies_by_name)
        for model in creation_order:
            # Pydantic resolves the models nested in a model with the namespace of the outermost model so we cannot
            # leave it to pydantic to build the copies when they are first used (even with defer_build=True)
            self.copies[model].model_rebuild(raise_errors=True, _types_namespace={})
            if not self.copies[model].__pydantic_complete__:  # pragma: no cover # raise_errors=True raises before
                raise SchemaGenerationError(f"Cadwyn could not build the versioned copy of {model.__qualname__}")
        for model_copy in self.copies.values():
            # Pydantic has already resolved the forward refs so the annotations can refer to the copies themselves,
            # e.g. for typing.get_type_hints
            annotations = vars(model_copy)["__annotations__"]
            for name, annotation in annotations.items():
                annotations[name] = _replace_forward_refs(annotation, copies_by_name)
        return self.copies


_BUILTIN_CONTAINER_TYPES = (list, dict, set, tuple, frozenset)
# The functions behind pydantic's own constructors, which create instances of the class that they are bound to
_PYDANTIC_CONSTRUCTORS = frozenset(
    vars(cls)[name].__func__
    for cls in (BaseModel, RootModel)
    for name in ("model_construct", "model_validate", "model_validate_json")
    if name in vars(cls)
)


def _is_pydantic_model_class(cls: type) -> bool:
    """Whether the class is BaseModel, RootModel, or a parametrization of RootModel such as RootModel[int]"""
    return cls in (BaseModel, RootModel) or getattr(cls, "__pydantic_generic_metadata__", {}).get("origin") is RootModel


def _iter_values_within(value: Any) -> "collections.abc.Iterator[Any]":
    """Iterate over the value and the values within it: in its containers and in the fields of its models"""
    values_to_visit = [value]
    visited_ids: set[int] = set()
    while values_to_visit:
        current_value = values_to_visit.pop()
        if id(current_value) in visited_ids:
            continue
        visited_ids.add(id(current_value))
        yield current_value
        if isinstance(current_value, dict):
            values_to_visit.extend(current_value.values())
        elif isinstance(current_value, list | tuple | set | frozenset):
            values_to_visit.extend(current_value)
        elif isinstance(current_value, BaseModel):
            values_to_visit.extend(vars(current_value).values())
            values_to_visit.extend((current_value.__pydantic_extra__ or {}).values())


def _replace_forward_refs(annotation: Any, models_by_name: dict[str, type[BaseModel]]) -> Any:
    """Replace the forward refs to the models in the type positions of the annotation (see _CycleCopier)"""
    return _replace_in_type_positions(
        annotation,
        lambda value: models_by_name.get(value.__forward_arg__, value) if isinstance(value, ForwardRef) else value,
    )


def _replace_in_type_positions(annotation: Any, replace: Callable[[Any], Any]) -> Any:
    """Replace the values in the type positions of the annotation, keeping the annotation if nothing changes"""
    replaced = replace(annotation)
    if replaced is not annotation:
        return replaced
    if isinstance(annotation, list):
        # E.g. the args of Callable[[Item], None]
        return [_replace_in_type_positions(arg, replace) for arg in annotation]
    if not isinstance(annotation, (*GenericAliasUnionArgs, UnionType)):
        return annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    new_args = tuple(
        _replace_in_type_positions(arg, replace) if _is_type_position_in_generic(origin, index) else arg
        for index, arg in enumerate(args)
    )
    if all(new_arg is arg for new_arg, arg in zip(new_args, args, strict=True)):
        return annotation
    if isinstance(annotation, UnionType):
        return typing.Union[new_args]
    return _rebuild_generic_alias(annotation, new_args)


@cache
def generate_versioned_models(versions: "VersionBundle") -> "dict[str, SchemaGenerator]":
    models = _create_model_bundle(versions)
    changed_models = frozenset[type[Union[BaseModel, Enum]]]().union(
        *(_get_models_with_instructions(version) for version in versions._all_versions)
    )

    version_to_context_map = {}
    reference_cycles = _ReferenceCycles(changed_models)
    context = _RuntimeSchemaGenContext(current_version=versions.head_version, models=models)
    alias_changes = _migrate_classes(context)
    instructions_that_differ_from_head = _get_instructions_by_model(versions.head_version, {})

    for version in versions.versions:
        _warn_about_alias_changes_that_keep_old_keys(alias_changes, str(version.value))
        context = _RuntimeSchemaGenContext(current_version=version, models=models)
        version_to_context_map[str(version.value)] = SchemaGenerator(
            copy.deepcopy(models), changed_models, reference_cycles, instructions_that_differ_from_head
        )
        # note that the last migration will not contain any version changes so we don't need to save the results
        alias_changes = _migrate_classes(context)
        instructions_that_differ_from_head = _get_instructions_by_model(version, instructions_that_differ_from_head)

    return version_to_context_map


def _get_instructions_by_model(
    version: "Union[Version, HeadVersion]",
    instructions_of_newer_versions: "collections.abc.Mapping[type[Union[BaseModel, Enum]], tuple[Any, ...]]",
) -> "dict[type[Union[BaseModel, Enum]], tuple[Any, ...]]":
    instructions_by_model = dict(instructions_of_newer_versions)
    for version_change in version.changes:
        instructions = [
            *((instruction.schema, instruction) for instruction in version_change.alter_schema_instructions),
            *((instruction.enum, instruction) for instruction in version_change.alter_enum_instructions),
        ]
        for model, instruction in instructions:
            instructions_by_model[model] = (*instructions_by_model.get(model, ()), instruction)
    return instructions_by_model


def _only_changes_fields(instruction: Any, field_names: "collections.abc.Set[str]") -> bool:
    """Whether the schema instruction only changes some of these fields, without renaming them"""
    if isinstance(instruction, FieldHadInstruction):
        return instruction.name in field_names and instruction.new_name in {Sentinel, instruction.name}
    return (
        isinstance(instruction, FieldDidntHaveInstruction | FieldDidntExistInstruction | FieldExistedAsInstruction)
        and instruction.name in field_names
    )


def _get_models_validated_by_fields(model: type[BaseModel]) -> set[type[Union[BaseModel, Enum]]]:
    """Return the models and enums in the types of the resolved fields of the model, including typed extras"""
    fields = _get_rebuilt_model_fields(model)
    annotations = [field.annotation for field in (model.model_fields if fields is None else fields).values()]
    if model.model_config.get("extra") == "allow":
        annotations.append(_get_extra_fields_annotation(model))
    collector = _ReferencedModelCollector(expands_type_aliases=True)
    for annotation in annotations:
        collector.annotation_transformer.change_version_of_annotation(annotation)
    return collector.models | collector.arg_models


def _get_extra_fields_annotation(model: type[BaseModel]) -> Any:
    """Return the annotation of `__pydantic_extra__` that types the extra fields of the model, if any"""
    for cls in model.__mro__:
        if not lenient_issubclass(cls, BaseModel):
            continue
        annotation = _get_annotations_without_evaluating(cls).get("__pydantic_extra__")
        if annotation is not None:
            return _try_eval_type_for_discovery(annotation, _get_module_globals(cls))
    return None


def _get_module_globals(cls: type) -> dict[str, Any]:
    module = sys.modules.get(cls.__module__)
    return {} if module is None else vars(module)


class _TypeAliasValue(NamedTuple):
    alias: Any
    args: tuple[Any, ...]
    value: Any


def _are_same_args(args: tuple[Any, ...], other_args: tuple[Any, ...]) -> bool:
    try:
        return bool(args == other_args)
    except Exception:  # noqa: BLE001  # pragma: no cover # E.g. Annotated metadata whose __eq__ raises
        return False


def _get_value_of_type_alias_with_args(annotation: Any) -> "Union[_TypeAliasValue, Any]":
    """Return the type alias and its value with the args of the annotation, or _UNRESOLVED if we cannot.

    E.g. `list[int]` for `Ints[int]` with `type Ints[T] = list[T]`, or for `Ints` with `type Ints = list[int]`.
    """
    from typing_inspection.typing_objects import is_typealiastype

    if is_typealiastype(annotation):
        alias, args = annotation, ()
    elif isinstance(annotation, GenericAliasUnionArgs) and is_typealiastype(get_origin(annotation)):
        alias, args = get_origin(annotation), get_args(annotation)
    else:
        return _UNRESOLVED
    try:
        args_by_param = dict(zip(alias.__type_params__, args, strict=True))
        value = _get_type_alias_value(alias)
        if isinstance(value, TypeVar):
            value = args_by_param[value]
        elif getattr(value, "__parameters__", ()):
            value = value[tuple(args_by_param[param] for param in value.__parameters__)]
    except (NameError, KeyError, TypeError, ValueError):
        # E.g. its value uses names that are not defined yet, or it has a TypeVarTuple that takes several args
        return _UNRESOLVED
    return _TypeAliasValue(alias, args, value)


def _get_models_with_instructions(version: "Union[Version, HeadVersion]") -> "frozenset[type[Union[BaseModel, Enum]]]":
    return frozenset(
        instruction.schema
        for version_change in version.changes
        for instruction in version_change.alter_schema_instructions
    ) | frozenset(
        instruction.enum for version_change in version.changes for instruction in version_change.alter_enum_instructions
    )


def _create_model_bundle(versions: "VersionBundle"):
    return _ModelBundle(
        enums={enum: _EnumWrapper(enum) for enum in versions.versioned_enums.values()},
        schemas={schema: _wrap_pydantic_model(schema) for schema in versions.versioned_schemas.values()},
    )


class _AliasChange(NamedTuple):
    model: _PydanticModelWrapper[BaseModel]
    field: PydanticFieldWrapper
    instruction: Union[FieldHadInstruction, FieldDidntHaveInstruction]
    version_change_name: str


def _migrate_classes(context: _RuntimeSchemaGenContext) -> list[_AliasChange]:
    alias_changes: list[_AliasChange] = []
    for version_change in context.current_version.changes:
        alias_changes += _apply_alter_schema_instructions(
            context.models.schemas,
            version_change.alter_schema_instructions,
            version_change.__name__,
        )
        _apply_alter_enum_instructions(
            context.models.enums,
            version_change.alter_enum_instructions,
            version_change.__name__,
        )
    return alias_changes


_ALIAS_ATTRIBUTES = frozenset(("alias", "alias_priority", "validation_alias", "serialization_alias"))


def _warn_about_alias_changes_that_keep_old_keys(alias_changes: list[_AliasChange], version: str) -> None:
    """Warn about instructions that change the alias of a field but leave the keys that pydantic copied from it.

    Pydantic copies `alias` into `validation_alias` and `serialization_alias` unless they are passed, and Cadwyn
    passes them on unchanged, so the field keeps using its old keys. Cadwyn 9.0 will change them too (#465).
    """
    for model, field, instruction, version_change_name in alias_changes:
        field_name = next((name for name, model_field in model.fields.items() if model_field is field), None)
        old_alias = field.declared_field_attributes.get("alias")
        # If in doubt, we don't warn: e.g. alias generators can replace the aliases of the field
        if field_name is None or old_alias is None or model.cls.model_config.get("alias_generator") is not None:
            continue
        # The aliases of the field in the copy of the model: pydantic merges its Field() with the one in Annotated
        attributes = field.get_field_attributes(model.cls)
        field_info = FieldInfo.from_annotated_attribute(
            model.annotations[field_name],
            Field(**{name: value for name, value in attributes.items() if name in _ALIAS_ATTRIBUTES}),
        )
        validation_alias = field_info.validation_alias
        # FastAPI documents the fields whose validation_alias is AliasChoices or AliasPath by their alias
        if field_info.alias == old_alias or (validation_alias is not None and not isinstance(validation_alias, str)):
            continue
        # Pydantic stores the copied aliases in the same way as the ones that were passed with the same value
        kept_aliases = [
            name
            for name in ("validation_alias", "serialization_alias")
            if getattr(field_info, name) == old_alias
            and not any(change.name == name and issubclass(model.cls, change.changed_by) for change in field.changes)
        ]
        if not kept_aliases:
            continue
        alias = field_info.alias
        if isinstance(instruction, FieldHadInstruction):
            method, alias_part = "had", f"alias={instruction.field_changes.alias!r}"
            recipe = " and ".join(f"{name}={alias!r}" for name in kept_aliases)
        else:
            method, alias_part = "didnt_have", '"alias"'
            recipe = " and ".join(f'"{name}"' for name in kept_aliases)
        new_key = "the key that the field has without an alias" if alias is None else f'"{alias}"'
        model_name, is_plural = instruction.schema.__name__, len(kept_aliases) > 1
        warnings.warn(
            f'schema({model_name}).field("{instruction.name}").{method}({alias_part}) in "{version_change_name}" '
            f'changes the alias of "{model_name}.{instruction.name}", but in API version "{version}" its '
            f'{" and ".join(kept_aliases)} {"are" if is_plural else "is"} still "{old_alias}", so that version keeps '
            f'using "{old_alias}" instead of {new_key}. Cadwyn 9.0 will change {"them" if is_plural else "it"} too. '
            f"To use {new_key} now and silence this warning, also pass {recipe} to this .{method}(). "
            f'To keep "{old_alias}", remove {alias_part} from it instead.',
            FutureWarning,
            stacklevel=2,
        )


def _apply_alter_schema_instructions(
    modified_schemas: dict[type[BaseModel], _PydanticModelWrapper[BaseModel]],
    alter_schema_instructions: Sequence[Union[AlterSchemaSubInstruction, SchemaHadInstruction]],
    version_change_name: str,
) -> list[_AliasChange]:
    alias_changes: list[_AliasChange] = []
    for alter_schema_instruction in alter_schema_instructions:
        schema_info = modified_schemas[alter_schema_instruction.schema]
        if isinstance(alter_schema_instruction, FieldExistedAsInstruction):
            _add_field_to_model(schema_info, modified_schemas, alter_schema_instruction, version_change_name)
        elif isinstance(alter_schema_instruction, FieldHadInstruction | FieldDidntHaveInstruction):
            field = _change_field_in_model(
                schema_info,
                modified_schemas,
                alter_schema_instruction,
                version_change_name,
            )
            if (
                alter_schema_instruction.field_changes.alias is not Sentinel
                if isinstance(alter_schema_instruction, FieldHadInstruction)
                else "alias" in alter_schema_instruction.attributes
            ):
                alias_changes.append(_AliasChange(schema_info, field, alter_schema_instruction, version_change_name))
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
    return alias_changes


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
    name = alter_schema_instruction.name
    description = alter_schema_instruction.description
    if name is not Sentinel and name == model.name:
        raise InvalidGenerationInstructionError(
            f'You tried to change the name of "{model.name}" in "{version_change_name}" '
            "but it already has the name you tried to assign.",
        )
    if description is not Sentinel and _get_description(description) == _get_description(model.doc):
        if description:
            raise InvalidGenerationInstructionError(
                f'You tried to change the description of "{model.name}" in "{version_change_name}" '
                "but it already has the description you tried to assign.",
            )
        raise InvalidGenerationInstructionError(
            f'You tried to remove the description of "{model.name}" in "{version_change_name}" '
            "but it already doesn't have a description.",
        )

    if name is not Sentinel:
        model.name = name
    if description is not Sentinel:
        model.doc = description


def _get_description(docstring: Union[str, None]) -> Union[str, None]:
    """Return the description that pydantic generates from the docstring of a model"""
    return inspect.cleandoc(docstring) if docstring else None


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
) -> PydanticFieldWrapper:
    defined_annotations = model._get_defined_annotations_through_mro(schemas)
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name not in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to change the field "{alter_schema_instruction.name}" from '
            f'"{model.name}" in "{version_change_name}" but it doesn\'t have such a field.',
        )

    field = defined_fields[alter_schema_instruction.name]
    model.fields[alter_schema_instruction.name] = field
    if alter_schema_instruction.name not in model.annotations:
        defined_annotations[alter_schema_instruction.name] = _substitute_type_vars_of_inherited_annotation(
            model, schemas, alter_schema_instruction.name, defined_annotations[alter_schema_instruction.name]
        )
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
        if alter_schema_instruction.new_name is not Sentinel:
            _make_subclasses_follow_rename(
                model, schemas, field, alter_schema_instruction.name, alter_schema_instruction.new_name
            )
    else:
        _delete_field_attributes(
            model,
            alter_schema_instruction,
            version_change_name,
            field,
            model.annotations[alter_schema_instruction.name],
        )
    return field


def _substitute_type_vars_of_inherited_annotation(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    field_name: str,
    annotation: Any,
) -> Any:
    """Substitute the type vars in the annotation of a field that the model inherits, as pydantic does.

    Pydantic substitutes the type vars of the inherited fields of the parametrizations of generics (e.g. `second: U`
    of `Pair` in `Pair[str, int]` and in `class StrIntPair(Pair[str, int])`) but not of the fields that a class
    defines itself. So when the copy of the model defines such a field because of an instruction, it gets the
    annotation of its parent with the type vars that the parametrizations between them substitute.
    """
    parent = next(parent for parent in model._get_parents(schemas) if field_name in parent.annotations).cls
    # Like pydantic, each class inherits the field from its first base that has it. The other bases, e.g. `A[int]`
    # in `class M(A[int], B[T])`, do not substitute its type vars even if they reuse them.
    classes_on_the_way: list[type] = []
    cls: type = model.cls
    while cls is not parent:
        classes_on_the_way.append(cls)
        cls = next(base for base in cls.__bases__ if lenient_issubclass(base, parent))
    # From the parametrization closest to the parent to the model itself, as pydantic substitutes them
    for cls in reversed(classes_on_the_way):
        origin = getattr(cls, "__pydantic_generic_metadata__", {}).get("origin")
        if origin is not None:
            parameters = origin.__pydantic_generic_metadata__["parameters"]
            type_vars = dict(zip(parameters, _eval_generic_args(cast("type[BaseModel]", cls)), strict=True))
            substituted_annotation = replace_types(annotation, type_vars)
            # Pydantic rebuilds Annotated even without type vars in it, which would give it another cache key
            if substituted_annotation != annotation:
                annotation = substituted_annotation
    return annotation


def _get_subclasses_that_share_field(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    field: PydanticFieldWrapper,
    field_name: str,
) -> "list[_PydanticModelWrapper[BaseModel]]":
    return [
        subclass
        for subclass in schemas.values()
        if subclass is not model
        and issubclass(subclass.cls, model.cls)
        and (subclass.fields.get(field_name) is field or subclass.fields_renamed_by_parents.get(field_name) is field)
    ]


def _make_subclasses_follow_rename(
    model: _PydanticModelWrapper[BaseModel],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    field: PydanticFieldWrapper,
    old_name: str,
    new_name: str,
) -> None:
    """Make the subclasses that changed the field apply their changes to it under its new name too.

    Otherwise, they would only inherit the renamed field of the model, which does not have their changes.
    """
    for subclass in _get_subclasses_that_share_field(model, schemas, field, old_name):
        subclass.fields_renamed_by_parents.pop(old_name, None)
        subclass.fields_renamed_by_parents[new_name] = field


def _get_parent_that_renamed_field(
    model: type[BaseModel],
    field_name: str,
    field: PydanticFieldWrapper,
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
) -> Union[type[BaseModel], None]:
    """Return the parent that the model inherits the renamed field from, if the model can define it instead.

    The model can only define it if it would inherit it through single inheritance from that parent without any
    generics on the way (pydantic substitutes their type args) and no other class on the way declares the name
    in any way. Otherwise, we leave the name to pydantic. The parent could also have reused or deleted the name
    since it renamed the field.
    """
    cls = model
    while len(cls.__bases__) == 1 and not _is_generic_model(cls) and not _declares_name(cls, field_name, schemas):
        base = cls.__bases__[0]
        if not issubclass(base, BaseModel) or base is BaseModel or _is_generic_model(base):
            return None
        if base in schemas and field_name in schemas[base].fields:
            # Each model has its own copy of the field in every version
            return base if schemas[base].fields[field_name].identity == field.identity else None
        cls = base
    return None


def _declares_name(
    model: type[BaseModel], name: str, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]"
) -> bool:
    """Whether the model or its versioned copy declares the name as anything: a field, a class var, a property..."""
    wrapper = schemas.get(model)
    if wrapper is not None and (
        name in wrapper.annotations or name in wrapper.other_attributes or name in wrapper.validators
    ):
        return True
    return name in _get_annotations_without_evaluating(model) or name in vars(model)


def _is_parametrization(model: type[BaseModel]) -> bool:
    """Whether the model is the parametrization of a generic, e.g. `Page[Item]` but not `Page` or `ItemPage`"""
    return model.__pydantic_generic_metadata__["origin"] is not None


def _get_models_in_field(field: FieldInfo) -> Union[frozenset[type[Union[BaseModel, Enum]]], None]:
    """Return the models that copying the field requests (see _change_version_of_model_references), if we know them"""
    if _contains_pydantic_recursive_refs(field.annotation):
        # Pydantic refers to the parametrizations that it is still building (e.g. `children: list[Node[T]]` within
        # `Node[str]`) by their pydantic references, which do not lead to the copies, so we do not know the models
        return None
    collector = _ReferencedModelCollector()
    transformer = collector.annotation_transformer
    with transformer.hard_position():
        transformer.change_version_of_annotation(_extract_passed_field_attributes(field))
    transformer.change_version_of_annotation(_get_annotation_with_metadata(field))
    return frozenset(collector.models) if collector.is_complete else None


def _get_annotation_with_metadata(field: FieldInfo) -> Any:
    return _AnnotatedAlias(field.annotation, tuple(field.metadata)) if field.metadata else field.annotation


def _contains_pydantic_recursive_refs(annotation: Any) -> bool:
    if isinstance(annotation, PydanticRecursiveRef):
        return True
    # E.g. the list of args in Callable[[Item], None]
    args = annotation if isinstance(annotation, list) else get_args(annotation)
    return any(_contains_pydantic_recursive_refs(arg) for arg in args)


def _is_generic_model(model: type[BaseModel]) -> bool:
    """Whether the model is generic (e.g. `Parent[T]`) or parametrized (e.g. `Parent[int]`)"""
    generic_metadata = model.__pydantic_generic_metadata__
    return generic_metadata["origin"] is not None or bool(generic_metadata["parameters"])


def _change_field(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
    annotation: Union[Any, None],
):
    if alter_schema_instruction.type is not Sentinel:
        if field.annotation == alter_schema_instruction.type and (
            field.type_changed_by is None
            or issubclass(model.cls, field.type_changed_by)
            # Another model that shares the field (e.g. a sibling) could have changed its type only for itself
            or annotation == alter_schema_instruction.type
        ):
            raise InvalidGenerationInstructionError(
                f'You tried to change the type of field "{alter_schema_instruction.name}" to '
                f'"{alter_schema_instruction.type}" from "{model.name}" in "{version_change_name}" '
                f'but it already has type "{field.annotation}"',
            )
        field.annotation = alter_schema_instruction.type
        field.type_changed_by = model.cls
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
            shared_value = field.passed_field_attributes.get(attr_name, Sentinel)
            # Another model that shares the field (e.g. a sibling) could have set the value only for itself.
            # We compare the shared value first, as Cadwyn always did, and avoid comparing it with the model's own
            # value because user defaults can compare in any way (e.g. numpy arrays compare element-wise)
            if shared_value == attr_value and (
                (model_value := field.get_field_attributes(model.cls).get(attr_name, Sentinel)) is shared_value
                or model_value == attr_value
            ):
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )
            # Like Cadwyn always did, we don't change the annotation's metadata, so a constraint from it (e.g. from
            # `Annotated[str, Field(min_length=1)]`) still wins over the new value. Changing it would make the older
            # versions reject the requests and responses that they have always accepted whenever it is stricter.
            field.update_attribute(name=attr_name, value=attr_value, changed_by=model.cls)


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


def _metadata_has_attribute(metadata: Any, attr_name: str) -> bool:
    """Whether field().didnt_have() deletes the attribute from the metadata"""
    if isinstance(metadata, FieldInfo):
        return hasattr(metadata, attr_name) or attr_name in _extract_passed_field_attributes(metadata)
    if inspect.isclass(metadata):
        # pydantic also allows constraint classes such as `Annotated[int, Strict]`
        return attr_name in collect_known_metadata([metadata])[0]
    # Only attributes that can be set on the object itself, so not str.title from `Annotated[str, "Some doc"]`
    return hasattr(metadata, attr_name) and (
        hasattr(metadata, "__dict__")
        or any(attr_name in getattr(cls, "__slots__", ()) for cls in type(metadata).__mro__)
    )


def _delete_metadata_attribute(metadata: Any, attr_name: str) -> Any:
    if isinstance(metadata, FieldInfo):
        # We only delete the attributes of FieldInfo itself (never its constraints), and they get the values of an
        # empty Field()
        field_info = _copy_field_info(metadata)
        field_info._attributes_set.pop(attr_name, None)
        setattr(field_info, attr_name, dict_of_empty_field_info[attr_name])
        return field_info
    unset_value = _get_unset_metadata_attribute_value(metadata, attr_name)
    if inspect.isclass(metadata):
        # We never change the class itself and use its instance with the same constraints instead
        constraint = metadata.__new__(metadata)
        for name, value in collect_known_metadata([metadata])[0].items():
            object.__setattr__(constraint, name, value)
    else:
        # We don't use dataclasses.replace because it would call __init__ that subclasses can override
        constraint = copy.copy(metadata)
    object.__setattr__(constraint, attr_name, unset_value)
    return constraint


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


# The attributes that field().didnt_have() removes from a Field() in the annotation's metadata, such as
# `Annotated[str, Field(description="...")]`. Cadwyn 7.4.0 left that Field() unchanged, so we only remove the
# attributes that only document the field: removing any other one could make the older versions reject or change
# the data that they have always accepted or returned. E.g. removing a default makes the field required, removing an
# alias renames it, removing exclude returns a hidden field, and even removing a constraint such as max_length can
# make a model-wide limit (`ConfigDict(str_max_length=...)`) or an older had() apply, or change the member that a
# `left_to_right` union picks. json_schema_extra holds arbitrary data that validators can read, so it stays as well.
_FIELD_INFO_ATTRIBUTES_REMOVED_BY_DIDNT_HAVE = frozenset(("title", "description", "examples", "deprecated"))


def _metadata_attribute_can_be_deleted(metadata: Any, attr_name: str) -> bool:
    if isinstance(metadata, FieldInfo) and attr_name not in _FIELD_INFO_ATTRIBUTES_REMOVED_BY_DIDNT_HAVE:
        return False
    return _metadata_has_attribute(metadata, attr_name)


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
                functools.partial(_metadata_attribute_can_be_deleted, attr_name=attr_name),
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
