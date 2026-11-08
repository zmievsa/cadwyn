import collections.abc
import contextlib
import copy
import dataclasses
import functools
import inspect
import itertools
import reprlib
import sys
import threading
import types
import typing
from collections.abc import Callable, Generator, Sequence
from datetime import date
from enum import Enum
from functools import cache
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    ForwardRef,
    Generic,
    NamedTuple,
    TypeAlias,
    Union,
    assert_never,
    cast,
    final,
    get_args,
    get_origin,
    overload,
)

import fastapi.params
import fastapi.security.base
import fastapi.utils
import pydantic
import pydantic._internal._decorators
from fastapi import Response
from pydantic import BaseModel, RootModel
from pydantic._internal._config import ConfigWrapper
from pydantic._internal._fields import rebuild_model_fields
from pydantic._internal._forward_ref import PydanticRecursiveRef
from pydantic._internal._generics import PydanticGenericMetadata, replace_types
from pydantic._internal._model_construction import build_lenient_weakvaluedict, unpack_lenient_weakvaluedict
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import (
    Doc,
    Protocol,
    TypeVar,
    _AnnotatedAlias,
)

from cadwyn._asts import GenericAliasUnionArgs
from cadwyn._utils import (
    DATACLASS_KW_ONLY,
    DATACLASS_SLOTS,
    Sentinel,
    UnionType,
    _callable_name,
    _get_type_alias_value,
    get_name_of_function_wrapped_in_pydantic_validator,
    is_same_value,
    lenient_issubclass,
)
from cadwyn.exceptions import CadwynError, InvalidGenerationInstructionError, SchemaGenerationError
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import ResponseInfo
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
    _KEYS_RENAMED_BY_ALIAS,
    VersionBundle,
    _get_schema_migration_target,
    _reject_keys_renamed_by_alias,
    _rescue_model_data,
)

if TYPE_CHECKING:
    from cadwyn.structure.versions import HeadVersion, Version


from ._annotations import (
    _NO_REDEFINED_FIELDS,
    _UNRESOLVED,
    _are_same_args,
    _contains_forward_refs,
    _contains_only_builtin_types,
    _copy_field_info,
    _eval_forward_refs,
    _evaluate_user_annotations,
    _get_annotation_with_metadata,
    _get_annotations_without_evaluating,
    _get_extra_fields_annotation,
    _get_model_annotations,
    _get_model_field_names,
    _get_rebuilt_model_fields,
    _get_value_of_type_alias_with_args,
    _has_undefined_names_in_lazy_annotations,
    _is_type_position_in_generic,
    _rebuild_generic_alias,
    _rebuild_model,
    _replace_forward_refs,
    _replace_in_type_positions,
    _try_eval_type,
    _try_eval_type_for_discovery,
    _TypeAliasRefersToItselfError,
)
from ._callables import (
    _add_request_and_response_params,
    _AsyncCallableWrapper,
    _AsyncGeneratorCallableWrapper,
    _CallableKind,
    _CallableWrapper,
    _GeneratorCallableWrapper,
    _get_best_effort_globals,
    _get_function_behind_callable,
    _get_globals,
    _get_signature,
    _get_unevaluated_signature,
    is_async_gen_callable,
    is_coroutine_callable,
    is_gen_callable,
    is_regular_function,
)
from ._common import (
    _PYDANTIC_ALL_EXPORTED_NAMES,
    _T_ANY_MODEL,
    _get_deep_copy_error,
    _is_dunder,
    _is_versionable_model,
    _unwrap_model,
)
from ._core_schema import (
    _get_classes_in_core_schema,
    _get_core_schema_definitions,
    _get_field_core_schemas,
)
from ._enums import (
    _apply_alter_enum_instructions,
    _EnumWrapper,
)
from ._fields import (
    _OTHER_DEFAULT_ATTRIBUTE,
    PydanticFieldWrapper,
    _change_annotated_metadata,
    _delete_metadata_attribute,
    _extract_passed_field_attributes,
    _metadata_attribute_can_be_deleted,
    _metadata_has_attribute,
    _PerFieldValidatorWrapper,
    _sets_default_attribute,
    _ValidatorWrapper,
    _wrap_validator,
)
from ._generics import (
    _alias_value_uses_fallbacks,
    _change_version_of_generic_metadata,
    _eval_generic_args,
    _get_typevar_uses,
    _is_generic_model,
    _is_parametrization,
    _is_same_parametrization,
)

_Call = TypeVar("_Call", bound=Callable[..., Any])

_FieldName: TypeAlias = str

_T_PYDANTIC_MODEL = TypeVar("_T_PYDANTIC_MODEL", bound=BaseModel)


@dataclasses.dataclass(**DATACLASS_SLOTS)
class _ModelBundle:
    enums: dict[type[Enum], "_EnumWrapper[Enum]"]
    schemas: dict[type[BaseModel], "_PydanticModelWrapper[BaseModel]"]
    models_with_config_instructions: frozenset[type[BaseModel]]

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ModelBundle":
        memos_of_models: dict[type[BaseModel], dict[int, Any]] = collections.defaultdict(dict)
        result = _ModelBundle(
            enums=copy.deepcopy(self.enums, memo),
            schemas={
                model: wrapper.copy_with_schemas(self.schemas, memos_of_models)
                for model, wrapper in self.schemas.items()
            },
            models_with_config_instructions=self.models_with_config_instructions,
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
        head_route=None,
        response_target=_get_schema_migration_target(latest_response_model),
    )

    versioned_response_model: type[pydantic.BaseModel] = generate_versioned_models(versions)[str(version)][
        latest_response_model
    ]
    # The body has the keys of `model_dump(by_alias=True)`, like the bodies that routes migrate. Like routes, we only
    # restore its validation aliases if it surely fails as it is, and we validate it once, whichever body it is,
    # because validators can have side effects.
    return versioned_response_model.model_validate(_rescue_model_data(versioned_response_model, migrated_response.body))


if "config_wrapper" not in inspect.signature(rebuild_model_fields).parameters:  # pragma: no cover
    raise ImportError(
        "Cadwyn does not support this version of pydantic: `rebuild_model_fields` of `pydantic._internal._fields` "
        "doesn't take `config_wrapper`. Cadwyn uses it to resolve the fields of models without rebuilding them. "
        "Please, open an issue on Cadwyn's GitHub."
    )


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
    transformer._model_owners.append(model)
    try:
        with transformer.hard_position():
            versioned_field_attributes = {}
            for name, attributes in field_attributes.items():
                with transformer.describing(f"the field '{name}' of {model.__qualname__}"):
                    versioned_field_attributes[name] = transformer.change_version_of_annotation(attributes)
        # __orig_bases__ keeps bases such as Generic[T] that __bases__ loses so that the copy stays generic
        orig_bases = tuple(
            generator.change_version_of_base(base) if isinstance(base, type) else base
            for base in model.__dict__.get("__orig_bases__", model.__bases__)
        )
        if isinstance(generator, _ReferencedModelCollector):
            _change_version_of_model_bases(generator, model, orig_bases)
        versioned_annotations = transformer.change_version_of_annotation(annotations)
        with transformer.hard_position():
            generic_metadata = generator.change_version_of_generic_metadata(model)
        return versioned_field_attributes, orig_bases, versioned_annotations, generic_metadata
    finally:
        transformer._model_owners.pop()


def _change_version_of_model_bases(
    generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier]",
    model: type[BaseModel],
    orig_bases: tuple[Any, ...],
) -> tuple[type, ...]:
    original_bases = model.__dict__.get("__orig_bases__", model.__bases__)
    if "__orig_bases__" not in model.__dict__:
        return orig_bases
    has_user_hooks = any(
        not isinstance(base, type)
        and get_origin(base) not in (Generic, typing.Protocol, Protocol)
        and inspect.getattr_static(base, "__mro_entries__", None) is not None
        for base in original_bases
    )
    if isinstance(generator, _ReferencedModelCollector):
        # Discovery never runs user hooks. Only their original, actual parents may need versioning.
        if has_user_hooks:
            for base in model.__bases__:
                if issubclass(base, BaseModel) and not any(base is original for original in original_bases):
                    generator.change_version_of_base(base)
        return model.__bases__
    if not has_user_hooks:
        return types.resolve_bases(orig_bases)

    schema_generator = generator.schema_generator if isinstance(generator, _CycleCopier) else generator
    try:
        bases = schema_generator._resolved_model_bases[model]
    except KeyError:
        # Use main's inputs and run each hook once per class in this version, including the fallback path.
        bases = types.resolve_bases(orig_bases)
        schema_generator._resolved_model_bases[model] = bases
    if not schema_generator._instructions_that_differ_from_head:
        return bases
    originals_by_copy_id = {
        id(base_copy): original
        for original, base_copy in zip(original_bases, orig_bases, strict=True)
        if isinstance(original, type)
    }
    if len(bases) != len(model.__bases__) or any(
        originals_by_copy_id.get(id(base), base) is not actual
        for base, actual in zip(bases, model.__bases__, strict=True)
    ):
        return bases
    # A known, complete parent with schema instructions can be replaced safely. Uncertain shapes keep main's
    # resolved bases; no transitive discovery or speculative startup errors are needed.
    return tuple(
        generator.change_version_of_base(base)
        if issubclass(base, BaseModel)
        and id(base) not in originals_by_copy_id
        and base.__pydantic_fields_complete__
        and any(parent in schema_generator._instructions_that_differ_from_head for parent in base.__mro__)
        else base
        for base in bases
    )


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
    config_changed_keys: frozenset[str] = frozenset()
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
            config_changed_keys=self.config_changed_keys,
            fields_renamed_by_parents=fields_renamed_by_parents,
        )

    def get_config_changes(self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]") -> dict[str, Any]:
        """Return this model's config patches, including patches of a parametrization's generic origin.

        Pydantic parametrizations store a snapshot of their origin's config. Only keys changed by instructions
        follow the versioned origin; instructions on the parametrization itself take precedence.
        """
        origin = self.cls.__pydantic_generic_metadata__["origin"]
        inherited_changes = (
            schemas[origin].get_config_changes(schemas) if origin is not None and origin in schemas else {}
        )
        config = self.other_attributes["model_config"]
        return inherited_changes | {key: config[key] for key in self.config_changed_keys}

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
        defined_fields = self.fields_renamed_by_parents | self.fields
        keys_renamed_by_alias = {
            name: defined_fields[name].keys_renamed_by_alias
            for name in field_attributes
            if defined_fields[name].keys_renamed_by_alias
        }
        if keys_renamed_by_alias:
            # Pydantic makes it a classmethod because its first parameter is "cls"
            root_validators[_REJECT_KEYS_RENAMED_BY_ALIAS] = pydantic.model_validator(mode="wrap")(
                _reject_keys_renamed_by_alias
            )

        # Keep hook execution at main's construction point, after copying field models and building fields.
        bases = _change_version_of_model_bases(generator, self.cls, orig_bases)
        attributes = self.other_attributes
        if config_changes := self.get_config_changes(generator.model_bundle.schemas):
            attributes = attributes | {"model_config": attributes["model_config"] | config_changes}
        model_copy = type(self.cls)(
            self.name,
            bases,
            attributes
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
        typed_model_copy = cast("type[_T_PYDANTIC_MODEL]", model_copy)
        _set_keys_renamed_by_alias(typed_model_copy, field_attributes, keys_renamed_by_alias)
        return typed_model_copy


_REJECT_KEYS_RENAMED_BY_ALIAS = "__cadwyn_reject_keys_renamed_by_alias__"


def _set_keys_renamed_by_alias(
    model: type[BaseModel],
    defined_field_names: "collections.abc.Iterable[str]",
    keys_by_field: dict[str, frozenset[str]],
) -> None:
    """Set the keys that the fields of the model lost because their aliases followed alias.

    The model inherits them for the fields that it inherits from its bases without defining them. Each model class
    leaves out the keys that its own fields read (see `_get_renamed_key_checks`) when it first validates data.
    """
    defined_field_names = frozenset(defined_field_names)
    keys_by_field = {
        name: renamed_keys
        for base in reversed(model.__bases__)
        for name, renamed_keys in getattr(base, _KEYS_RENAMED_BY_ALIAS, {}).items()
        if name not in defined_field_names
    } | keys_by_field
    # The model also inherits the check and the keys of its bases, so it needs keys of its own if they have any
    if keys_by_field or hasattr(model, _KEYS_RENAMED_BY_ALIAS):
        setattr(model, _KEYS_RENAMED_BY_ALIAS, keys_by_field)


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
        self.typevar_copies: dict[typing.TypeVar, typing.TypeVar] = {}
        self._typevars_being_copied: set[typing.TypeVar] = set()
        self._typevar_owners: list[Any] = []
        self._model_owners: list[type[BaseModel]] = []
        self._typevar_scope_model_depth = 0
        self._template_position_depth = 0
        self._typevar_fallback_depth = 0
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
        # The ids of the type aliases that we are copying right now, to detect the ones that refer to themselves
        self._type_aliases_being_copied: set[int] = set()
        # The ids of the lists, tuples, and dicts that we are copying right now with whether they contain themselves
        self._containers_being_copied: dict[int, bool] = {}
        # How many classes and callables we have replaced with this version's copies, so that we can tell whether
        # anything within a container gets replaced. Typing objects, such as `list[int]`, get rebuilt even when
        # nothing within them gets replaced, so we cannot tell it by comparing the results with the originals.
        self._replacement_count = 0
        # How many containers that get deep copied as a whole we are in (see _change_version_of_container)
        self._only_checking_depth = 0
        # The places that we are versioning right now (e.g. fields), innermost last (see describing)
        self._locations: list[str] = []
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
            return self._change_version_of_container(annotation)

        elif isinstance(annotation, list | tuple):
            if self.record_only:
                self._record_without_cycles(annotation, lambda: annotation)
                return annotation
            return self._change_version_of_container(annotation)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation)

    def _change_version_of_container(self, container: Union[dict[Any, Any], list[Any], tuple[Any, ...]]) -> Any:
        """Return this version's own copy of the container, with this version's copies of the classes within it.

        Containers include the defaults of fields and params, so every version must get its own copies of them, as
        pydantic gives every instance its own copy of a mutable default. Plain lists, tuples, and dicts get rebuilt
        with their versioned values, as Cadwyn always did. We cannot rebuild other containers (e.g. subclasses, named
        tuples, or containers that contain themselves) without changing them, so they get deep copies, which follow
        their own __deepcopy__ and __reduce__, if nothing within them gets replaced (see _replacement_count).
        Otherwise, we fail instead of using the latest classes or giving them another type.
        """
        if id(container) in self._containers_being_copied:
            # It contains itself, so we deep copy it as a whole once we know that nothing within it gets replaced
            self._containers_being_copied[id(container)] = True
            return container
        self._containers_being_copied[id(container)] = False
        replacement_count = self._replacement_count
        is_plain = type(container) in {dict, list, tuple}
        try:
            # We deep copy other containers as a whole, so that their own __deepcopy__ decides how to copy the values
            # within them (e.g. it can keep the ones that cannot be copied), and we only check these values
            with contextlib.nullcontext() if is_plain else self._only_checking_containers():
                # The (key, value) pairs of dicts
                values: list[Any] = (
                    [
                        (self.change_version_of_annotation(key), self.change_version_of_annotation(value))
                        for key, value in container.items()
                    ]
                    if isinstance(container, dict)
                    else [self.change_version_of_annotation(value) for value in container]
                )
        finally:
            contains_itself = self._containers_being_copied.pop(id(container))
        if is_plain and not contains_itself:
            if isinstance(container, dict):
                return dict(values)
            return values if isinstance(container, list) else tuple(values)
        if self._replacement_count != replacement_count:
            reason = (
                "it contains itself (directly or through other values)"
                if contains_itself
                else f"it is a {type(container).__qualname__}, not a plain list, tuple, or dict"
            )
            raise SchemaGenerationError(
                f"Cadwyn cannot generate the versioned copy of {self._describe(container)} because {reason}, and it "
                "contains classes or functions that the version replaces with its own copies (e.g. models or enums "
                "with instructions, the models that use them, or functions). Cadwyn cannot rebuild it with these "
                "copies without changing it. Please use a plain list, tuple, or dict that does not contain itself."
            )
        if self._only_checking_depth:
            return container
        try:
            return copy.deepcopy(container)
        except Exception as error:
            raise _get_deep_copy_error(self._describe(container), error) from error

    def _describe(self, container: Any) -> str:
        return " in ".join([reprlib.repr(container), *self._locations[-1:]])

    @contextlib.contextmanager
    def _only_checking_containers(self) -> Generator[None, None, None]:
        """Check the containers within a container that gets deep copied as a whole instead of copying them"""
        self._only_checking_depth += 1
        try:
            yield
        finally:
            self._only_checking_depth -= 1

    @contextlib.contextmanager
    def describing(self, location: str) -> Generator[None, None, None]:
        """Name the place that we are versioning right now (e.g. a field) in the errors about the values within it"""
        self._locations.append(location)
        try:
            yield
        finally:
            self._locations.pop()

    def migrate_router_to_version(self, router: fastapi.routing.APIRouter):
        from cadwyn.route_generation import _DELETED_ROUTE_TAG

        for route in router.routes:
            if not isinstance(route, fastapi.routing.APIRoute) or _DELETED_ROUTE_TAG in route.tags:
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
        elif (
            isinstance(annotation, typing.TypeVar)
            and self._typevar_owners
            and not self._template_position_depth
            and self._typevar_scope_model_depth == len(self._model_owners)
        ):
            return self._change_version_of_typevar(annotation)
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
                return self._copy_type_alias(annotation)
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

    @contextlib.contextmanager
    def template_position(self) -> Generator[None, None, None]:
        """Copy type references that do not themselves validate values, without binding their TypeVars."""
        self._template_position_depth += 1
        try:
            yield
        finally:
            self._template_position_depth -= 1

    @contextlib.contextmanager
    def typevar_namespace(self, owner: Any) -> Generator[None, None, None]:
        if (
            self._typevar_owners
            and self._typevar_owners[-1] is owner
            and self._typevar_scope_model_depth == len(self._model_owners)
        ):
            yield
            return
        self._typevar_owners.append(owner)
        saved_depth = self._typevar_scope_model_depth
        self._typevar_scope_model_depth = len(self._model_owners)
        saved_copies, saved_active = self.typevar_copies, self._typevars_being_copied
        self.typevar_copies, self._typevars_being_copied = {}, set()
        try:
            yield
        finally:
            self.typevar_copies, self._typevars_being_copied = saved_copies, saved_active
            self._typevar_scope_model_depth = saved_depth
            self._typevar_owners.pop()

    def _change_version_of_typevar(self, param: typing.TypeVar) -> typing.TypeVar:
        if param in self.typevar_copies:
            return self.typevar_copies[param]
        if param in self._typevars_being_copied:
            if self.record_only:
                self._record_unknown_references()
                return param
            raise SchemaGenerationError(
                f"Cadwyn cannot version type parameter {param.__name__} because its bounds, constraints, or "
                "default refer back to it. Use explicit type arguments instead."
            )
        self._typevars_being_copied.add(param)
        try:
            replacement_count = self._replacement_count
            bound = None
            constraints = ()
            default_kwargs = {}
            default = getattr(param, "__default__", None)
            has_default = getattr(param, "has_default", lambda: default is not None)()
            if has_default:
                default_kwargs["default"] = self._version_typevar_annotation(param, default)
            elif param.__constraints__:
                constraints = tuple(self._version_typevar_annotation(param, value) for value in param.__constraints__)
            elif param.__bound__ is not None:
                bound = self._version_typevar_annotation(param, param.__bound__)
            if self.record_only:
                return param
            if self._replacement_count == replacement_count:
                copied = param
            else:
                # Runtime reconstruction uses the third-party factory dynamically; static TypeVar syntax
                # cannot express bounds, constraints, and defaults discovered at runtime.
                factory = sys.modules["typing_extensions"].TypeVar
                copied = factory(
                    param.__name__,
                    *constraints,
                    bound=bound,
                    covariant=param.__covariant__,
                    contravariant=param.__contravariant__,
                    infer_variance=getattr(param, "__infer_variance__", False),
                    **default_kwargs,
                )
                copied.__module__ = param.__module__
            self.typevar_copies[param] = copied
            return copied
        finally:
            self._typevars_being_copied.remove(param)

    def _version_typevar_annotation(self, param: typing.TypeVar, value: Any) -> Any:
        if _contains_forward_refs(value):
            owner = self._typevar_owners[-1]
            if isinstance(owner, type) and issubclass(owner, BaseModel):
                value = _eval_forward_refs(value, owner)
            else:
                value = _try_eval_type(value, vars(sys.modules[owner.__module__]))
            if _contains_forward_refs(value):
                if self.record_only:
                    self._record_unknown_references()
                    return value
                raise SchemaGenerationError(
                    f"Cadwyn cannot resolve {value!r} in type parameter {param.__name__}. "
                    "Define the referenced types before generating versions, or use explicit type arguments."
                )
        generator = self.generator.schema_generator if isinstance(self.generator, _CycleCopier) else self.generator
        if isinstance(generator, SchemaGenerator):
            collector = _ReferencedModelCollector()
            collector.annotation_transformer._typevar_owners = self._typevar_owners.copy()
            collector.annotation_transformer._typevar_fallback_depth = self._typevar_fallback_depth + 1
            collector.annotation_transformer.change_version_of_annotation(value)
            if collector.is_complete and not any(generator._is_versioned(model) for model in collector.models):
                # Match explicit generic arguments: unchanged models keep their identity and behavior.
                return value
        # A TypeVar must be built before pydantic sees it, so cyclic bounds need the classes themselves.
        self._typevar_fallback_depth += 1
        try:
            with self.hard_position():
                versioned_value = self.change_version_of_annotation(value)
        finally:
            self._typevar_fallback_depth -= 1
        if not self.record_only:
            self._check_typevar_value(param, versioned_value)
        return versioned_value

    def _check_typevar_value(self, param: typing.TypeVar, value: Any) -> None:
        """Reject bounds/defaults whose copied generics still validate a changed HEAD model.

        Generic arguments can deliberately keep HEAD models to avoid reference cycles. Type parameters must
        not silently inherit that fallback, including when the generic was already copied and cached.
        """
        from typing_inspection.typing_objects import is_typealiastype

        generator = self.generator.schema_generator if isinstance(self.generator, _CycleCopier) else self.generator
        if not isinstance(generator, SchemaGenerator) or not generator._instructions_that_differ_from_head:
            return
        values = [value]
        visited: set[int] = set()
        while values:
            current = values.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, type) and issubclass(current, BaseModel):
                for cls in _get_classes_in_core_schema(current.__pydantic_core_schema__):
                    if cls in generator._instructions_that_differ_from_head or (
                        issubclass(cls, BaseModel)
                        and any(generator._base_differs_from_head(cls, index) for index in range(1, len(cls.__mro__)))
                    ):
                        raise SchemaGenerationError(
                            f"Cadwyn cannot version type parameter {param.__name__}: its bound, constraints, or "
                            f"default still validate the latest {cls.__qualname__} in an older version. "
                            "Use explicit type arguments with models that Cadwyn can version."
                        )
            elif is_typealiastype(current):
                values.append(_get_type_alias_value(current))
            else:
                origin = get_origin(current)
                if is_typealiastype(origin):
                    values.append(origin)
                values.extend(get_args(current))

    def _copy_type_alias(self, alias: Any) -> Any:
        """Return the copy of the type alias whose value refers to this version's copies of models.

        A type alias that refers to itself (e.g. `type Json = dict[str, Json] | int`), directly or through other type
        aliases, cannot be copied because its copy would have to exist before its value. So we use the type alias
        itself if it only contains builtin types (see _contains_only_builtin_types), which are the same in every
        version, and fail otherwise.
        """
        if id(alias) in self._type_aliases_being_copied:
            raise _TypeAliasRefersToItselfError(alias)
        self._type_aliases_being_copied.add(id(alias))
        try:
            with self.typevar_namespace(alias):
                value = self.change_version_of_annotation(_get_type_alias_value(alias))
                type_params = tuple(self.typevar_copies.get(param, param) for param in alias.__type_params__)
        except _TypeAliasRefersToItselfError as error:
            if error.alias is not alias:
                raise
            if _contains_only_builtin_types(alias, visited_aliases=set()):
                return alias
            raise SchemaGenerationError(
                f"Cadwyn cannot generate the versioned copies of the type alias {alias.__name__} because it refers "
                "to itself. Type aliases that refer to themselves are only supported when they only contain builtin "
                "types (e.g. `type JsonData = dict[str, JsonData] | list[JsonData] | str | int | None`) and other "
                "such type aliases. Classes (e.g. models and enums), forward refs, and Annotated are not supported."
            ) from None
        finally:
            self._type_aliases_being_copied.discard(id(alias))
        return type(alias)(name=alias.__name__, value=value, type_params=type_params)

    def _change_version_of_callable(self, annotation: Callable[..., Any], *, callable_kind: _CallableKind) -> Any:
        # Callables, such as default factories, are called with the classes themselves and their annotations are
        # not resolved by pydantic
        with (
            self.hard_position(),
            self.template_position() if callable_kind is _CallableKind.OTHER else contextlib.nullcontext(),
        ):
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
            self._replacement_count += 1
            with self.describing(f"the parameters of {_callable_name(annotation)}"):
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
                call, signature, evaluate=_try_eval_type_for_discovery
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
        with self.typevar_namespace(alias):
            self._record_without_cycles(alias, lambda: [_get_type_alias_value(alias)])

    def _record_unknown_references(self) -> None:
        """Record that we cannot know every model that copying the annotation would request.

        E.g. because the annotation refers to names that are not defined yet, which could refer to any model
        once they get defined, or because copying it would never finish.
        """
        cast("_ReferencedModelCollector", self.generator).is_complete = False

    def _change_version_of_generic_alias(self, annotation: Any) -> Any:
        from typing_inspection.typing_objects import is_typealiastype

        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is ClassVar or origin is collections.abc.Callable:
            with self.template_position():
                new_args = tuple(
                    self._change_version_of_generic_arg(origin, index, arg) for index, arg in enumerate(args)
                )
            return annotation if self.record_only else _rebuild_generic_alias(annotation, new_args)
        if is_typealiastype(origin):
            # The alias value can hold a bare generic even when none of the args contains a model.
            versioned_args = self.change_version_of_annotation(args)
            if not self._typevar_fallback_depth and not _alias_value_uses_fallbacks(origin, len(args)):
                return (
                    annotation
                    if self.record_only
                    else origin[versioned_args[0] if len(versioned_args) == 1 else versioned_args]
                )
            supplied_params = origin.__type_params__[: len(args)]
            if self.record_only:
                with self.typevar_namespace(origin):
                    self.typevar_copies.update({param: param for param in supplied_params})
                    self.change_version_of_annotation(origin)
                return annotation
            collector = _ReferencedModelCollector()
            # Supplied arguments replace these parameters, so their bounds/defaults do not validate anything.
            with collector.annotation_transformer.typevar_namespace(origin):
                collector.annotation_transformer.typevar_copies.update({param: param for param in supplied_params})
                collector.annotation_transformer.change_version_of_annotation(origin)
            versioned_origin = origin
            generator = self.generator.schema_generator if isinstance(self.generator, _CycleCopier) else self.generator
            if isinstance(generator, SchemaGenerator) and any(
                generator._is_versioned(model) for model in collector.models
            ):
                with self.typevar_namespace(origin):
                    self.typevar_copies.update({param: param for param in supplied_params})
                    versioned_origin = self.change_version_of_annotation(origin)
            return versioned_origin[versioned_args[0] if len(versioned_args) == 1 else versioned_args]
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
            if (self._template_position_depth or self._is_template_reference(annotation)) and not isinstance(
                self.generator, _ReferencedModelCollector
            ):
                depth = self._template_position_depth
                self._template_position_depth = 0
                try:
                    if isinstance(self.generator, SchemaGenerator):
                        versioned_class = self.generator._get_template(annotation)
                    elif _unwrap_model(annotation) not in self.generator._forward_refs:
                        versioned_class = self.generator.schema_generator._get_template(annotation)
                    else:
                        versioned_class = self.generator[annotation]
                finally:
                    self._template_position_depth = depth
            else:
                versioned_class = self.generator[annotation]
            if versioned_class is not annotation:
                self._replacement_count += 1
            return versioned_class
        else:
            return annotation

    def _is_template_reference(self, annotation: type) -> bool:
        """Keep nested generics sharing the enclosing model's parameters as templates.

        Binding Inner[T] while copying Outer[T] would prevent Outer[int] from substituting int,
        and would inspect a bound which that concrete parametrization never validates.
        """
        if not self._model_owners or not _is_versionable_model(annotation) or not issubclass(annotation, BaseModel):
            return False
        parameters = self._model_owners[-1].__pydantic_generic_metadata__["parameters"]
        return bool(set(parameters).intersection(annotation.__pydantic_generic_metadata__["parameters"]))

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
            callable_annotations = cls._get_annotations_of_callable_called_by_fastapi(call, old_signature)
            annotation_modifying_wrapper.__annotations__ = modify_annotations(callable_annotations)
            if return_annotation is not inspect.Signature.empty:
                # FastAPI validates the route's explicit response_model, which is versioned separately, and never
                # validates dependency returns. Copying these annotations can reject models that no route uses.
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
        *,
        evaluate: Union[Callable[[Any, dict[str, Any]], Any], None] = None,
    ) -> dict[str, Any]:
        """Return the annotations of an endpoint or a dependency that its versioned copy versions"""
        callable_annotations = {
            name: param.annotation
            for name, param in old_signature.parameters.items()
            if param.annotation is not inspect.Parameter.empty
        }
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
            if (
                isinstance(model, type)
                and issubclass(model, BaseModel)
                and model.__pydantic_generic_metadata__["parameters"]
                and not self.annotation_transformer._template_position_depth
                and not self.annotation_transformer._is_template_reference(model)
                and model not in self.annotation_transformer._model_owners
            ):
                transformer = self.annotation_transformer
                with transformer.typevar_namespace(model):
                    transformer._record_without_cycles(model, lambda: [tuple(_get_typevar_uses(model))])
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
        "_bare_models",
        "_bare_models_being_generated",
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
        "_parent_renames",
        "_reference_cycles",
        "_resolved_model_bases",
        "_root_models",
        "_route_versioning_thread",
        "_version",
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
        parent_renames: "tuple[_ParentRename, ...]",
        version: str,
    ) -> None:
        self.annotation_transformer = _AnnotationTransformer(self)
        self._bare_models: dict[type[BaseModel], type[BaseModel]] = {}
        self._bare_models_being_generated: set[type[BaseModel]] = set()
        self.model_bundle = model_bundle
        # The renames of this version and the newer ones, which the copies of the subclasses must follow
        self._parent_renames = parent_renames
        self._version = version
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
        self._resolved_model_bases: dict[type[BaseModel], tuple[type, ...]] = {}
        # Models are generated through __getitem__ so that the copies created along the way are reused
        # instead of being created again
        for model in [*self.model_bundle.schemas, *self.model_bundle.enums]:
            # Instructions describe templates too; they do not imply a bare validation use.
            self._get_template(model)

    def __getitem__(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
        if not self._changed_models or not _is_versionable_model(model) or not issubclass(model, BaseModel):
            return self._get_template(model)
        original = _unwrap_model(model)
        if not original.__pydantic_generic_metadata__["parameters"]:
            return self._get_template(model)
        # The cache preserves the original class relationship, like concrete_models below.
        return cast("type[_T_ANY_MODEL]", self._get_bare_model(original))

    def _get_bare_model(self, original: type[BaseModel]) -> type[BaseModel]:
        with self._lock:
            if original in self._bare_models:
                return self._bare_models[original]
            if original in self._bare_models_being_generated:
                raise SchemaGenerationError(
                    f"Cadwyn cannot version the bare generic {original.__qualname__} because its type parameter "
                    "fallbacks refer back to it. Use explicit type arguments."
                )
            self._bare_models_being_generated.add(original)
            try:
                parameters = original.__pydantic_generic_metadata__["parameters"]
                template = self._get_template(original)
                used = _get_typevar_uses(template)
                transformer = self.annotation_transformer
                args = []
                for param in parameters:
                    if param not in used:
                        args.append(param)
                        continue
                    owner = self._get_typevar_owner(param, used[param])
                    collector = _ReferencedModelCollector()
                    with collector.annotation_transformer.typevar_namespace(owner):
                        collector.annotation_transformer.change_version_of_annotation(param)
                    if original in collector.models:
                        # Self-bound parameters already follow main's recursive model copying. Keep the
                        # entire parameter unchanged, including constraints that mention other models.
                        args.append(param)
                        continue
                    with transformer.typevar_namespace(owner):
                        args.append(transformer._change_version_of_typevar(param))
                if all(arg is param for arg, param in zip(args, parameters, strict=True)):
                    self._bare_models[original] = template
                    return template
                # Specialize the complete source, then use the ordinary generator so instructions on nested
                # generics are applied too. The template keeps its original parameters for concrete uses.
                specialized = replace_types(original, dict(zip(parameters, args, strict=True)))
                if not isinstance(specialized, type) or not issubclass(specialized, BaseModel):
                    raise SchemaGenerationError(
                        f"Cadwyn cannot safely parametrize the bare generic {original.__qualname__}"
                    )
                namespace = (unpack_lenient_weakvaluedict(original.__pydantic_parent_namespace__) or {}) | {
                    original.__name__: original,
                    **{param.__name__: arg for param, arg in zip(parameters, args, strict=True)},
                }
                specialized.__pydantic_parent_namespace__ = build_lenient_weakvaluedict(namespace)
                specialized.model_rebuild(_types_namespace={})
                result = self._get_template(specialized)
                setattr(result, "__cadwyn_original_model__", original)  # noqa: B010 # Runtime marker on generated class.
                self._bare_models[original] = result
                return result
            finally:
                self._bare_models_being_generated.remove(original)

    def _get_typevar_owner(self, param: typing.TypeVar, owners: set[type[BaseModel]]) -> type[BaseModel]:
        """Choose the namespace that actually validates a parameter, rejecting incompatible bare uses."""
        owners = {_unwrap_model(owner) for owner in owners}
        owner = next(iter(owners))
        default = getattr(param, "__default__", None)
        if getattr(param, "has_default", lambda: default is not None)():
            values = (default,)
        else:
            values = param.__constraints__ or (param.__bound__,)
        for value in values:
            if not _contains_forward_refs(value):
                continue
            resolved = [_eval_forward_refs(value, context) for context in owners]
            if all(target == resolved[0] for target in resolved):
                continue
            collector = _ReferencedModelCollector()
            for target in resolved:
                collector.annotation_transformer.change_version_of_annotation(target)
            if any(self._is_versioned(model) for model in collector.models):
                raise SchemaGenerationError(
                    f"Cadwyn cannot version the bare generic {owner.__qualname__}: type parameter "
                    f"{param.__name__} resolves differently across nested model namespaces. "
                    "Use explicit type arguments."
                )
        return owner

    def _get_template(self, model: type[_T_ANY_MODEL], /) -> type[_T_ANY_MODEL]:
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
                    if lenient_issubclass(model_copy, BaseModel):
                        self._check_that_copy_follows_parent_renames(cast("type[BaseModel]", model), model_copy)
                    if self._copied_cycles and lenient_issubclass(model_copy, BaseModel):
                        self._check_copy_outside_of_cycles(cast("type[BaseModel]", model), model_copy)
                    self.concrete_models[model] = model_copy
                else:
                    self._copy_cycle(cycle)
            return cast("type[_T_ANY_MODEL]", self.concrete_models[model])

    def change_version_of_base(self, base: type[_T_ANY_MODEL]) -> type[_T_ANY_MODEL]:
        return self._get_template(base)

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
        if self.model_bundle.models_with_config_instructions and lenient_issubclass(model, BaseModel):
            original = _unwrap_model(model)
            origin = original.__pydantic_generic_metadata__["origin"]
            for base in original.__mro__[1:]:
                if base in self.model_bundle.models_with_config_instructions and base is not origin:
                    raise InvalidGenerationInstructionError(
                        f'Cannot version config of "{base.__name__}" because "{model.__name__}" is a subclass '
                        "among the app's versioned models. Pydantic does not retain whether a child's config was "
                        "inherited or explicitly overridden. Put the config instruction on each subclass "
                        "(leaf model) instead."
                    )
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

    def _check_that_copy_follows_parent_renames(self, model: type[BaseModel], model_copy: type[BaseModel]) -> None:
        """Fail if the copy of a subclass has a field that a parent renamed under another name next to the new one.

        Pydantic models can't drop the fields that they inherit, so a subclass keeps the field under the old name if
        it declares it itself (or changed it, which makes its copy declare it) or inherits it from another base, and
        inherits it under the new name from the parent. We decide from the copy that pydantic built, so only real
        fields count, not e.g. class vars. Only the fields that instructions added since the rename may have the old
        name, in the version of the rename and in the older ones.
        """
        for rename in self._parent_renames:
            parent, old_name = rename.parent, rename.old_name
            if model is parent or not issubclass(model, parent):
                continue
            new_name = _get_name_of_field(self.model_bundle.schemas[parent], rename.field_identity)
            if new_name in {None, old_name} or not self._gets_renamed_field_from_parent(
                model, model_copy, rename, new_name
            ):
                continue
            # The parent got a field with the old name back, or still inherits it, which the renames of the parent
            # itself report once all models of the version are copied
            if _copy_of_parent_has_field(model_copy, parent, old_name):
                continue
            for name in self._get_names_of_own_copies_of_renamed_field(model, rename):
                if name != new_name and name in model_copy.model_fields:
                    raise _get_parent_rename_error(model, model_copy, rename, name, new_name, self._version)
            if (
                old_name in model_copy.model_fields
                and old_name not in _get_annotations_without_evaluating(model_copy)
                # Like pydantic, we skip `object`
                and any(self._declared_field_before_rename(base, rename) for base in model.__mro__[1:-1])
            ):
                raise _get_parent_rename_error(model, model_copy, rename, old_name, new_name, self._version)

    def _gets_renamed_field_from_parent(
        self, model: type[BaseModel], model_copy: type[BaseModel], rename: "_ParentRename", new_name: str
    ) -> bool:
        """Whether the copy of the model has the field that the parent renamed under the new name.

        A field of the model itself with that name hides it, unless it is the model's copy of the parent's field
        (e.g. because the model changed it). So does a class before the copy of the parent in the MRO that declares
        the name, e.g. a first base.
        """
        if new_name not in model_copy.model_fields:
            return False
        if new_name in _get_annotations_without_evaluating(model_copy):
            wrapper = self.model_bundle.schemas.get(model)
            return wrapper is not None and any(
                field is not None and field.identity == rename.field_identity
                for field in (wrapper.fields.get(new_name), wrapper.fields_renamed_by_parents.get(new_name))
            )
        # The closest class that is the copy of the parent or declares the name. Like pydantic, we skip `object`
        closest = next(
            (
                cls
                for cls in model_copy.__mro__[1:-1]
                if vars(cls).get("__cadwyn_original_model__") is rename.parent
                or new_name in _get_annotations_without_evaluating(cls)
            ),
            None,
        )
        return closest is not None and vars(closest).get("__cadwyn_original_model__") is rename.parent

    def _get_names_of_own_copies_of_renamed_field(self, model: type[BaseModel], rename: "_ParentRename") -> list[str]:
        """Return the names of the fields of the model that are its own copies of the field that the parent renamed.

        That is the field that it declared under the old name before the version, and the parent's field itself if
        the model changed it, under their current names. A model without instructions keeps its declarations.
        """
        if model not in rename.models_with_instructions:
            return [rename.old_name] if rename.old_name in _get_annotations_without_evaluating(model) else []
        identities = {rename.field_identity, rename.field_identities_before_version[model].get(rename.old_name)}
        return [name for name, field in self.model_bundle.schemas[model].fields.items() if field.identity in identities]

    def _declared_field_before_rename(self, cls: type, rename: "_ParentRename") -> bool:
        """Whether the class declares a field with the old name that existed before the version of the rename"""
        if cls in rename.models_with_instructions:
            field = self.model_bundle.schemas[cast("type[BaseModel]", cls)].fields.get(rename.old_name)
            return field is not None and field.identity in rename.field_identities_of_version
        if rename.old_name not in _get_annotations_without_evaluating(cls):
            return False
        # Not a class var of a pydantic model
        return not lenient_issubclass(cls, BaseModel) or rename.old_name in cls.__pydantic_fields__

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
                self._check_that_copy_follows_parent_renames(model, copies[model])
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


def get_models_that_routes_request(routers: "collections.abc.Iterable[fastapi.routing.APIRouter]") -> set[type[Any]]:
    """Return the models that versioning the routes of the routers requests from a generator directly"""
    collector = _ReferencedModelCollector()
    for router in routers:
        for route in router.routes:
            if isinstance(route, fastapi.routing.APIRoute):
                collector.annotation_transformer.record_route(route)
    return collector.models


# Unique across all versions and all attempts so that the names of different copies never get mixed up, e.g.
# through the caches of typing: Optional[ForwardRef("X")] is cached so its ForwardRef("X") is shared.
_reference_cycle_member_ids = itertools.count()


def _get_sort_key_of_model(model: type[BaseModel]) -> tuple[str, str]:
    return model.__module__, model.__qualname__


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


@cache
def generate_versioned_models(versions: "VersionBundle") -> "dict[str, SchemaGenerator]":
    models = _create_model_bundle(versions)
    changed_models = frozenset[type[Union[BaseModel, Enum]]]().union(
        *(_get_models_with_instructions(version) for version in versions._all_versions)
    )

    version_to_context_map = {}
    reference_cycles = _ReferenceCycles(changed_models)
    context = _RuntimeSchemaGenContext(current_version=versions.head_version, models=models)
    field_identities_before_version = _get_field_identities(models.schemas)
    changes = _migrate_classes(context)
    first_renames = _get_first_renames(changes.renames)
    # The renames of the newer versions, which every older version must not undo by inheriting the old field
    renames_of_newer_versions = list(first_renames)
    parent_renames_of_newer_versions: list[_ParentRename] = []
    instructions_that_differ_from_head = _get_instructions_by_model(versions.head_version, {})

    for version in versions.versions:
        context = _RuntimeSchemaGenContext(current_version=version, models=models)
        parent_renames = _get_parent_renames(first_renames, field_identities_before_version)
        generator = SchemaGenerator(
            copy.deepcopy(models),
            changed_models,
            reference_cycles,
            instructions_that_differ_from_head,
            (*parent_renames_of_newer_versions, *parent_renames),
            str(version.value),
        )
        _check_that_renamed_fields_are_not_inherited(
            renames_of_newer_versions, models.schemas, generator, str(version.value)
        )
        _check_that_deleted_fields_are_not_inherited(changes.deleted_fields, generator, str(version.value))
        version_to_context_map[str(version.value)] = generator
        parent_renames_of_newer_versions += parent_renames
        # note that the last migration will not contain any version changes so we don't need to save the results
        field_identities_before_version = _get_field_identities(models.schemas)
        changes = _migrate_classes(context)
        first_renames = _get_first_renames(changes.renames)
        renames_of_newer_versions += first_renames
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
        models_with_config_instructions=frozenset(
            instruction.schema
            for version in versions._all_versions
            for change in version.changes
            for instruction in change.alter_schema_instructions
            if isinstance(instruction, SchemaHadInstruction) and instruction.config
        ),
    )


class _FieldRename(NamedTuple):
    model: _PydanticModelWrapper[BaseModel]
    field: PydanticFieldWrapper
    instruction: FieldHadInstruction
    version_change_name: str


class _ParentRename(NamedTuple):
    """The first rename of a field of a model in a version, which its subclasses must not undo by keeping the field"""

    parent: type[BaseModel]
    field_identity: int
    # The name of the field before the version
    old_name: str
    version_change_name: str
    # The models with instructions, and the identities of the fields that each of them declared before the version
    field_identities_before_version: "collections.abc.Mapping[type[BaseModel], collections.abc.Mapping[str, int]]"
    models_with_instructions: frozenset[type[BaseModel]]
    # The identities of all of these fields
    field_identities_of_version: frozenset[int]


class _DeletedField(NamedTuple):
    model: _PydanticModelWrapper[BaseModel]
    name: str
    version_change_name: str


class _SchemaChanges(NamedTuple):
    renames: list[_FieldRename]
    deleted_fields: list[_DeletedField]


def _migrate_classes(context: _RuntimeSchemaGenContext) -> _SchemaChanges:
    changes = _SchemaChanges(renames=[], deleted_fields=[])
    for version_change in context.current_version.changes:
        _apply_alter_schema_instructions(
            context.models.schemas,
            version_change.alter_schema_instructions,
            version_change.__name__,
            changes,
        )
        _apply_alter_enum_instructions(
            context.models.enums,
            version_change.alter_enum_instructions,
            version_change.__name__,
        )
    return changes


def _get_field_identities(
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
) -> dict[type[BaseModel], dict[str, int]]:
    """Return the identities of the fields that each model with instructions declares, by their names"""
    return {
        model: {name: field.identity for name, field in wrapper.fields.items()} for model, wrapper in schemas.items()
    }


def _get_parent_renames(
    first_renames: list[_FieldRename], field_identities_before_version: dict[type[BaseModel], dict[str, int]]
) -> list[_ParentRename]:
    """Return the renames of the version, with the fields that the models declared before any instruction of it.

    So the order of the instructions in the version doesn't matter, e.g. a subclass that renames the field to another
    name before or after the parent does.
    """
    models_with_instructions = frozenset(field_identities_before_version)
    field_identities_of_version = frozenset(
        identity for identities in field_identities_before_version.values() for identity in identities.values()
    )
    return [
        _ParentRename(
            rename.model.cls,
            rename.field.identity,
            rename.instruction.name,
            rename.version_change_name,
            field_identities_before_version,
            models_with_instructions,
            field_identities_of_version,
        )
        for rename in first_renames
    ]


def _get_first_renames(renames: list[_FieldRename]) -> list[_FieldRename]:
    """Return the first rename of each field in a version: it has the name that the field had before the version.

    So the names that a field has only in the middle of a version (e.g. `y` in `x -> y -> z`) don't count.
    """
    first_renames: dict[tuple[int, int], _FieldRename] = {}
    for rename in renames:
        first_renames.setdefault((id(rename.model), id(rename.field)), rename)
    return list(first_renames.values())


def _check_that_renamed_fields_are_not_inherited(
    renames: list[_FieldRename],
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    generator: "SchemaGenerator",
    version: str,
) -> None:
    """Fail if the copy of a model has a renamed field under both its old and its current name.

    Pydantic models can't drop the fields of their bases, so the copy of the model can inherit the old field from
    a base, in the version of the rename or in any older one (e.g. after a parent gets the field back). We look at
    the copies that pydantic built for the version, so only real fields count, not e.g. class vars.
    """
    for model, field, instruction, version_change_name in renames:
        old_name = instruction.name
        new_name = next((name for name, model_field in model.fields.items() if model_field is field), None)
        # The model deleted the renamed field or renamed it back
        if new_name in {None, old_name}:
            continue
        model_copy = generator[model.cls]
        if (
            old_name not in model_copy.model_fields
            or new_name not in model_copy.model_fields
            or old_name in _get_annotations_without_evaluating(model_copy)
        ):
            continue
        bases = _get_bases_that_give_inherited_field(model_copy, old_name)
        parents: dict[type, _PydanticModelWrapper[BaseModel]] = {
            parent.cls: parent for parent in model._get_parents(schemas)
        }
        model_name, base_name = model.cls.__name__, bases[0].__name__
        base_field = parents[bases[0]].fields.get(old_name) if bases[0] in parents else None
        if len(bases) > 1:
            advice = _UNKNOWN_BASE_ADVICE.format(action="rename")
        elif bases[0] not in parents:
            advice = (
                f'"{base_name}" is not a pydantic model, so Cadwyn can\'t rename the field in it. Declare the '
                f'field in a pydantic model instead of "{base_name}" and rename it there.'
            )
        elif base_field is not None and base_field.identity == field.identity:
            advice = (
                f'Rename it in "{base_name}" instead, which renames it in its subclasses too. Subclasses that '
                "declare the field themselves or changed it in a newer version need the same rename in the same "
                "version."
            )
        else:
            advice = f'Rename it in "{base_name}" in the same version too.'
        raise InvalidGenerationInstructionError(
            f'You tried to rename the field "{old_name}" of "{model_name}" to "{new_name}" in "{version_change_name}", '
            f'but in version "{version}" "{model_name}" still inherits "{old_name}" from {_format_bases(bases)}, so it '
            "would have both fields. Pydantic models can't drop the fields that they inherit. " + advice
        )


def _check_that_deleted_fields_are_not_inherited(
    deleted_fields: list[_DeletedField],
    generator: "SchemaGenerator",
    version: str,
) -> None:
    """Fail if the copy of a model still has a field that the model deleted, because it inherits it from a base.

    It happens when the model declares the field again or changed it in a newer version, and a base still has it.
    We look at the copies that pydantic built for the version, so only real fields count, not e.g. class vars.
    """
    for model, name, version_change_name in deleted_fields:
        model_copy = generator[model.cls]
        if name not in model_copy.model_fields or name in _get_annotations_without_evaluating(model_copy):
            continue
        bases = _get_bases_that_give_inherited_field(model_copy, name)
        model_name, base_name = model.cls.__name__, bases[0].__name__
        if len(bases) > 1:
            advice = _UNKNOWN_BASE_ADVICE.format(action="delete")
        elif lenient_issubclass(bases[0], BaseModel):
            advice = f'Delete it from "{base_name}" in the same version too, which deletes it from its subclasses too.'
        else:
            advice = (
                f'"{base_name}" is not a pydantic model, so Cadwyn can\'t delete the field from it. Declare the '
                f'field in a pydantic model instead of "{base_name}" and delete it there.'
            )
        raise InvalidGenerationInstructionError(
            f'You tried to delete the field "{name}" from "{model_name}" in "{version_change_name}", but in version '
            f'"{version}" "{model_name}" still inherits "{name}" from {_format_bases(bases)}, so it would still have '
            "the field. Pydantic models can't drop the fields that they inherit. " + advice
        )


def _copy_of_parent_has_field(model_copy: type[BaseModel], parent: type[BaseModel], field_name: str) -> bool:
    """Whether the copy of the parent among the bases of the copy of the model has the field"""
    return any(
        vars(base).get("__cadwyn_original_model__") is parent and field_name in getattr(base, "__pydantic_fields__", {})
        for base in model_copy.__mro__[1:]
    )


def _get_parent_rename_error(
    model: type[BaseModel],
    model_copy: type[BaseModel],
    rename: _ParentRename,
    name_in_model: str,
    new_name: str,
    version: str,
) -> InvalidGenerationInstructionError:
    model_name, parent_name = model.__name__, rename.parent.__name__
    start = (
        f'"{parent_name}" renamed the field "{rename.old_name}" to "{new_name}" in "{rename.version_change_name}", '
        f'but in version "{version}" "{model_name}" still has'
    )
    if name_in_model in _get_annotations_without_evaluating(model_copy):
        return InvalidGenerationInstructionError(
            f'{start} it as "{name_in_model}" next to "{new_name}", because "{model_name}" declares '
            f'"{rename.old_name}" itself or changed it in a newer version. The renames of a parent don\'t rename the '
            f'fields that its subclasses declare. Rename it in "{model_name}" in the same version too: '
            f'schema({model_name}).field("{name_in_model}").had(name="{new_name}").'
        )
    bases = _get_bases_that_give_inherited_field(model_copy, name_in_model)
    base_name = bases[0].__name__
    if len(bases) > 1:
        advice = _UNKNOWN_BASE_ADVICE.format(action="rename")
    elif lenient_issubclass(bases[0], BaseModel):
        advice = f'Rename it in "{base_name}" in the same version too.'
    else:
        advice = (
            f'"{base_name}" is not a pydantic model, so Cadwyn can\'t rename the field in it. Declare the field in a '
            f'pydantic model instead of "{base_name}" and rename it there.'
        )
    return InvalidGenerationInstructionError(
        f'{start} "{name_in_model}" next to "{new_name}", because it inherits "{name_in_model}" from '
        f"{_format_bases(bases)}. Pydantic models can't drop the fields that they inherit. " + advice
    )


def _get_name_of_field(model: _PydanticModelWrapper[BaseModel], field_identity: int) -> Union[str, None]:
    """Return the name of the field in the model, or None if the model doesn't declare it (any longer)"""
    return next((name for name, field in model.fields.items() if field.identity == field_identity), None)


def _get_bases_that_give_inherited_field(model_copy: type[BaseModel], field_name: str) -> list[type]:
    """Return the bases (their original classes, not their copies) that the copy of the model can get the field from.

    Pydantic gives a subclass a shallow copy of the field of its first direct base that has it, which shares the
    metadata (e.g. constraints) and the other attribute objects of the field of the base that declares it. So we
    return the only base that declares the name and whose field shares all of them. If several or none do (e.g. two
    bases that both declare `x: int`), we can't be sure, and we return all of the bases that declare the name,
    closest first. If no pydantic base declares such a field, pydantic built it from the closest annotation of the
    name, e.g. of a mixin.
    """
    inherited = model_copy.model_fields[field_name]
    # Like pydantic, we skip `object`, which has no annotations
    candidates = [base for base in model_copy.__mro__[1:-1] if field_name in _get_annotations_without_evaluating(base)]
    matching = [
        base
        for base in candidates
        if (declared := getattr(base, "__pydantic_fields__", {}).get(field_name)) is not None
        and _shares_attribute_objects(declared, inherited)
    ]
    bases = matching if len(matching) == 1 else candidates
    return list(dict.fromkeys(vars(base).get("__cadwyn_original_model__", base) for base in bases))


def _shares_attribute_objects(field: FieldInfo, other: FieldInfo) -> bool:
    return (
        field.annotation is other.annotation
        and field.default is other.default
        and field.default_factory is other.default_factory
        and field.description is other.description
        and field.json_schema_extra is other.json_schema_extra
        and len(field.metadata) == len(other.metadata)
        and all(
            metadata is other_metadata for metadata, other_metadata in zip(field.metadata, other.metadata, strict=True)
        )
    )


_UNKNOWN_BASE_ADVICE = (
    "Cadwyn can't tell which of them pydantic took the field from, so {action} it in all of them in the same version "
    "too."
)


def _format_bases(bases: list[type]) -> str:
    names = ", ".join(f'"{base.__name__}"' for base in bases)
    return names if len(bases) == 1 else f"one of {names}"


def _apply_alter_schema_instructions(
    modified_schemas: dict[type[BaseModel], _PydanticModelWrapper[BaseModel]],
    alter_schema_instructions: Sequence[Union[AlterSchemaSubInstruction, SchemaHadInstruction]],
    version_change_name: str,
    changes: _SchemaChanges,
) -> None:
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
            if isinstance(alter_schema_instruction, FieldHadInstruction) and (
                alter_schema_instruction.new_name is not Sentinel
            ):
                changes.renames.append(_FieldRename(schema_info, field, alter_schema_instruction, version_change_name))
        elif isinstance(alter_schema_instruction, FieldDidntExistInstruction):
            if alter_schema_instruction.name in schema_info.fields:
                changes.deleted_fields.append(
                    _DeletedField(schema_info, alter_schema_instruction.name, version_change_name)
                )
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
            _change_model(schema_info, alter_schema_instruction, version_change_name, modified_schemas)
        else:
            assert_never(alter_schema_instruction)


def _change_model(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: SchemaHadInstruction,
    version_change_name: str,
    schemas: dict[type[BaseModel], _PydanticModelWrapper[BaseModel]],
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
    if alter_schema_instruction.config:
        _change_model_config(model, alter_schema_instruction.config, version_change_name, schemas)


def _change_model_config(
    model: _PydanticModelWrapper[BaseModel],
    config_patch: pydantic.ConfigDict,
    version_change_name: str,
    schemas: dict[type[BaseModel], _PydanticModelWrapper[BaseModel]],
) -> None:
    config = model.other_attributes["model_config"] | model.get_config_changes(schemas)
    current = ConfigWrapper(config.copy(), check=False)
    # Pydantic <2.14 fills in the validation settings here; newer versions keep an effective config separately.
    current.core_config(None)
    if {"populate_by_name", "validate_by_alias", "validate_by_name"} & config_patch.keys():
        config.update(validate_by_alias=current.validate_by_alias, validate_by_name=current.validate_by_name)
    if "populate_by_name" in config_patch:
        # On older Pydantic releases these flags were copied into model_config at class creation. A patch of
        # populate_by_name must update them too, while letting explicitly patched validation flags take precedence.
        config.update(validate_by_alias=True, validate_by_name=config_patch["populate_by_name"])
    config.update(config_patch)
    updated = ConfigWrapper(config, check=False)
    updated.core_config(None)
    for key in config_patch:
        if is_same_value(getattr(current, key), getattr(updated, key)):
            raise InvalidGenerationInstructionError(
                f'You tried to change config of "{model.name}", key "{key}", in "{version_change_name}" '
                "but it already has the value you tried to assign."
            )
    model.other_attributes["model_config"] = config
    model.config_changed_keys |= frozenset(config_patch)
    if {"populate_by_name", "validate_by_alias", "validate_by_name"} & config_patch.keys():
        model.config_changed_keys |= {"validate_by_alias", "validate_by_name"}


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


def _contains_pydantic_recursive_refs(annotation: Any) -> bool:
    if isinstance(annotation, PydanticRecursiveRef):
        return True
    # E.g. the list of args in Callable[[Item], None]
    args = annotation if isinstance(annotation, list) else get_args(annotation)
    return any(_contains_pydantic_recursive_refs(arg) for arg in args)


def _change_field(
    model: _PydanticModelWrapper[BaseModel],
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
    annotation: Union[Any, None],
):
    if alter_schema_instruction.type is not Sentinel:
        if is_same_value(field.annotation, alter_schema_instruction.type) and (
            field.type_changed_by is None
            or issubclass(model.cls, field.type_changed_by)
            # Another model that shares the field (e.g. a sibling) could have changed its type only for itself
            or is_same_value(annotation, alter_schema_instruction.type)
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
            # Another model that shares the field (e.g. a sibling) could have set the value only for itself
            if is_same_value(field.passed_field_attributes.get(attr_name, Sentinel), attr_value) and is_same_value(
                field.get_field_attributes(model.cls).get(attr_name, Sentinel), attr_value
            ):
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.name}" '
                    f'of "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )
            # Like Cadwyn always did, we don't change the annotation's metadata, so a constraint from it (e.g. from
            # `Annotated[str, Field(min_length=1)]`) still wins over the new value. Changing it would make the older
            # versions reject the requests and responses that they have always accepted whenever it is stricter.
            field.update_attribute(name=attr_name, value=attr_value, changed_by=model.cls)


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
        if attr_name in field.passed_field_attributes or attr_name in field.get_changed_field_attributes(model.cls):
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
