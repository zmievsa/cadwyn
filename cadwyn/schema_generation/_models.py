import collections.abc  # noqa: TC003 # Needed by runtime annotation introspection.
import copy
import dataclasses
import functools
import inspect
import types
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    TypeAlias,
    Union,
    cast,
    final,
    get_args,
    get_origin,
)

import pydantic
from pydantic import BaseModel, RootModel
from pydantic._internal._fields import rebuild_model_fields
from pydantic._internal._forward_ref import PydanticRecursiveRef
from pydantic._internal._generics import PydanticGenericMetadata
from pydantic.fields import FieldInfo
from typing_extensions import (
    Doc,
    TypeVar,
    _AnnotatedAlias,
)

from cadwyn._utils import (
    DATACLASS_SLOTS,
    Sentinel,
    lenient_issubclass,
)
from cadwyn.structure.schemas import (
    FieldDidntExistInstruction,
    FieldDidntHaveInstruction,
    FieldExistedAsInstruction,
    FieldHadInstruction,
    _get_model_decorators,
)
from cadwyn.structure.versions._core_schema import (
    _KEYS_RENAMED_BY_ALIAS,
    _reject_keys_renamed_by_alias,
)

from ._annotation_evaluation import (
    _NO_REDEFINED_FIELDS,
    _eval_forward_refs,
    _get_annotation_with_metadata,
    _get_model_annotations,
    _get_model_field_names,
    _get_rebuilt_model_fields,
    _has_undefined_names_in_lazy_annotations,
    _rebuild_model,
)
from ._annotation_transformer import (
    _ModelReferences,
    _ReferencedModelCollector,
)
from ._common import (
    _is_dunder,
    _is_versionable_model,
)
from ._fields import (
    _OTHER_DEFAULT_ATTRIBUTE,
    PydanticFieldWrapper,
    _change_annotated_metadata,
    _delete_metadata_attribute,
    _extract_passed_field_attributes,
    _PerFieldValidatorWrapper,
    _sets_default_attribute,
    _ValidatorWrapper,
    _wrap_validator,
)
from ._generics import (
    _is_parametrization,
)
from ._instructions import (
    _get_parent_that_renamed_field,
)

if TYPE_CHECKING:
    from ._references import (
        _BareGenericCopier,
        _CycleCopier,
    )
    from ._schema_generator import (
        SchemaGenerator,
    )

_FieldName: TypeAlias = str

_T_PYDANTIC_MODEL = TypeVar("_T_PYDANTIC_MODEL", bound=BaseModel)


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
    generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier, _BareGenericCopier]",
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
        if generator.schema_generator is None:
            _change_version_of_model_bases(generator, model, orig_bases)
        # The TypeVars that are not the model's parameters validate values as their bounds, constraints, or defaults
        if transformer.versioned_parameters:
            # Quoted references such as "Node[T]" must not reach pydantic, which would parametrize the copy with
            # the original parameters. In the model's namespace, they are the model itself (see _BareGenericCopier)
            annotations = {name: _eval_forward_refs(annotation, model) for name, annotation in annotations.items()}
        with transformer.typevar_namespace(model):
            transformer.typevar_copies.update(
                {
                    param: transformer.versioned_parameters.get(param, param)
                    for param in model.__pydantic_generic_metadata__["parameters"]
                }
            )
            versioned_annotations = transformer.change_version_of_annotation(annotations)
        with transformer.hard_position():
            generic_metadata = generator.change_version_of_generic_metadata(model)
        return versioned_field_attributes, orig_bases, versioned_annotations, generic_metadata
    finally:
        transformer._model_owners.pop()


def _change_version_of_model_bases(
    generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier, _BareGenericCopier]",
    model: type[BaseModel],
    orig_bases: tuple[Any, ...],
) -> tuple[type, ...]:
    original_bases = model.__dict__.get("__orig_bases__", model.__bases__)
    if "__orig_bases__" not in model.__dict__:
        return orig_bases
    # Python has replaced every base that isn't a class through its __mro_entries__. Typing aliases such as
    # Generic[T] or Mixin[int] resolve the same way every time, so only the other objects are the user's hooks.
    # Annotated[Parent, ...] resolves to Parent, which we version from __bases__ just like the results of hooks.
    has_user_hooks = any(
        not isinstance(base, type) and get_origin(base) in (None, Annotated) for base in original_bases
    )
    if not has_user_hooks:
        return orig_bases if generator.schema_generator is None else types.resolve_bases(orig_bases)
    # Python has already run the user's hooks and put their results into __bases__, so we never run them again
    return tuple(
        generator.change_version_of_base(base) if issubclass(base, BaseModel) else base for base in model.__bases__
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

    def get_config(self, schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]") -> dict[str, Any]:
        """Return the model's config, including the keys that instructions changed in a parametrization's origin.

        Pydantic parametrizations store a snapshot of their origin's config. Instructions on the parametrization
        itself take precedence.
        """
        config = self.other_attributes["model_config"]
        origin = self.cls.__pydantic_generic_metadata__["origin"]
        if origin is None or origin not in schemas:
            return config
        origin_wrapper = schemas[origin]
        origin_config = origin_wrapper.other_attributes["model_config"]
        return config | {
            key: origin_config[key] for key in origin_wrapper.config_changed_keys - self.config_changed_keys
        }

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
        generator: "Union[SchemaGenerator, _ReferencedModelCollector, _CycleCopier, _BareGenericCopier]",
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

    def generate_model_copy(
        self, generator: "Union[SchemaGenerator, _CycleCopier, _BareGenericCopier]"
    ) -> type[_T_PYDANTIC_MODEL]:
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

        bases = _change_version_of_model_bases(generator, self.cls, orig_bases)
        attributes = self.other_attributes | {"model_config": self.get_config(generator.model_bundle.schemas)}
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
        # Pydantic took the namespace of this function. Quoted names that are still unresolved, such as the bound
        # of a TypeVar that names a class defined in a function, resolve in the model's own namespace instead
        typed_model_copy.__pydantic_parent_namespace__ = self.cls.__pydantic_parent_namespace__
        # The copies of reference cycles get rebuilt once all of them exist (see _CycleCopier)
        if (
            not typed_model_copy.__pydantic_complete__
            and not typed_model_copy.model_config.get("defer_build")
            and generator.schema_generator is generator
        ):
            typed_model_copy.model_rebuild(raise_errors=False, _types_namespace={})
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


def _only_changes_fields(instruction: Any, field_names: "collections.abc.Set[str]") -> bool:
    """Whether the schema instruction only changes some of these fields, without renaming them"""
    if isinstance(instruction, FieldHadInstruction):
        return instruction.name in field_names and instruction.new_name in {Sentinel, instruction.name}
    return (
        isinstance(instruction, FieldDidntHaveInstruction | FieldDidntExistInstruction | FieldExistedAsInstruction)
        and instruction.name in field_names
    )


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
