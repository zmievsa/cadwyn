import copy
import dataclasses
import functools
import inspect
import itertools
import reprlib
from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    NamedTuple,
    Union,
    get_args,
    get_origin,
)

import annotated_types
import pydantic
from pydantic import BaseModel, Field
from pydantic._internal import _decorators
from pydantic._internal._decorators import (
    FieldSerializerDecoratorInfo,
    FieldValidatorDecoratorInfo,
    ModelSerializerDecoratorInfo,
    ModelValidatorDecoratorInfo,
    RootValidatorDecoratorInfo,
    ValidatorDecoratorInfo,
)
from pydantic._internal._known_annotated_metadata import collect_known_metadata
from pydantic.fields import ComputedFieldInfo, FieldInfo
from pydantic_core import PydanticUndefined
from typing_extensions import (
    _AnnotatedAlias,
)

from cadwyn._utils import (
    DATACLASS_KW_ONLY,
    DATACLASS_SLOTS,
    Sentinel,
    fully_unwrap_decorator,
    is_same_value,
)
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import (
    FieldDidntHaveInstruction,
    FieldHadInstruction,
)

from ._annotation_evaluation import (
    _copy_field_info,
)
from ._common import (
    _get_deep_copy_error,
)

if TYPE_CHECKING:
    from ._models import (
        _PydanticModelWrapper,
    )

PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP: dict[type[_decorators.DecoratorInfo], Callable[..., Any]] = {
    ValidatorDecoratorInfo: pydantic.validator,  # ty: ignore[deprecated]  # Preserve Pydantic v1 migrations.
    FieldValidatorDecoratorInfo: pydantic.field_validator,
    FieldSerializerDecoratorInfo: pydantic.field_serializer,
    RootValidatorDecoratorInfo: pydantic.root_validator,  # ty: ignore[deprecated]  # Preserve Pydantic v1 migrations.
    ModelValidatorDecoratorInfo: pydantic.model_validator,
    ModelSerializerDecoratorInfo: pydantic.model_serializer,
    ComputedFieldInfo: pydantic.computed_field,
}


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
    # The aliases that Field() copied from alias (or that are equal to it). Like in Field(), they follow alias
    aliases_derived_from_alias: frozenset[str] = dataclasses.field(init=False)
    # The keys that the field got from its alias where it was declared (in head or in its existed_as()), which
    # can differ from the alias itself, e.g. if an alias generator overrides it
    keys_from_alias: frozenset[str] = dataclasses.field(init=False)
    # The keys from alias that a copy of the field doesn't have anymore because its derived aliases followed alias
    keys_renamed_by_alias: frozenset[str] = dataclasses.field(init=False, default=frozenset())
    changes: list[_FieldAttributeChange] = dataclasses.field(init=False)
    # The same for all copies of the field
    identity: int = dataclasses.field(init=False)
    models_that_changed_it: set[type[BaseModel]] = dataclasses.field(init=False)
    _field_attributes_by_model: dict[type[BaseModel], dict[str, Any]] = dataclasses.field(init=False, repr=False)

    def __post_init__(self, init_model_field: FieldInfo) -> None:
        self.passed_field_attributes = _extract_passed_field_attributes(init_model_field)
        self.declared_field_attributes = self.passed_field_attributes.copy()
        self.aliases_derived_from_alias = _get_aliases_derived_from_alias(self.declared_field_attributes)
        self.keys_from_alias = frozenset(
            key
            for name in self.aliases_derived_from_alias
            if (key := getattr(init_model_field, name)) == self.declared_field_attributes["alias"]
        )
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
            # Pydantic rejects declarations with both, so an instruction has set one of them
            changed_by = next(
                change.changed_by for change in reversed(self.changes) if change.name in {"default", "default_factory"}
            )
            raise InvalidGenerationInstructionError(
                f'Instructions leave the field "{name}" with both a default and a default_factory, which '
                "pydantic does not allow. The last instruction that changed one of them is for "
                f'"{changed_by.__name__}".'
            )
        field_copy = copy.copy(self)
        field_copy.annotation = copy.deepcopy(self.annotation, memo)
        field_copy.passed_field_attributes = {}
        for attr_name, attr_value in self.get_field_attributes(model).items():
            try:
                field_copy.passed_field_attributes[attr_name] = copy.deepcopy(attr_value, memo)
            except Exception as error:
                description = f"{reprlib.repr(attr_value)} in the field '{name}' of {model.__qualname__}"
                raise _get_deep_copy_error(description, error) from error
        field_copy.declared_field_attributes = field_copy.passed_field_attributes
        # Its attributes already have the aliases that followed alias
        field_copy.aliases_derived_from_alias = frozenset()
        field_copy.keys_renamed_by_alias = self._get_keys_renamed_by_alias(model)
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
        Like in Field(), the aliases that Field() copied from alias follow it unless an instruction changed them.
        """
        if model not in self._field_attributes_by_model:
            changes = self._get_changes(model)
            self._field_attributes_by_model[model] = _make_derived_aliases_follow_alias(
                self._apply_changes(changes), self._get_derived_aliases(changes)
            )
        return self._field_attributes_by_model[model]

    def get_changed_field_attributes(self, model: type[BaseModel]) -> dict[str, Any]:
        """Return the attributes of the field in the model as the instructions changed them, without following alias"""
        return self._apply_changes(self._get_changes(model))

    def _get_changes(self, model: type[BaseModel]) -> "list[_FieldAttributeChange]":
        applies = {changed_by: issubclass(model, changed_by) for changed_by in self.models_that_changed_it}
        return [change for change in self.changes if applies[change.changed_by]]

    def _get_derived_aliases(self, changes: "list[_FieldAttributeChange]") -> frozenset[str]:
        # The aliases that were changed explicitly don't follow alias anymore
        return self.aliases_derived_from_alias.difference(change.name for change in changes)

    def _get_keys_renamed_by_alias(self, model: type[BaseModel]) -> frozenset[str]:
        """Return the keys from alias that the field loses in the model because its derived aliases follow alias"""
        changes = self._get_changes(model)
        if not self._get_derived_aliases(changes):
            # E.g. a copy of the field, whose attributes already have the aliases that followed alias
            return self.keys_renamed_by_alias
        alias = self.get_field_attributes(model).get("alias")
        return frozenset() if alias == self.declared_field_attributes["alias"] else self.keys_from_alias

    def _apply_changes(self, changes: "list[_FieldAttributeChange]") -> dict[str, Any]:
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


def _get_aliases_derived_from_alias(field_attributes: dict[str, Any]) -> frozenset[str]:
    """Return the aliases that Field() set to alias because they were not passed (pydantic stores both the same way)"""
    if "alias" not in field_attributes:
        return frozenset()
    return frozenset(
        name
        for name in ("validation_alias", "serialization_alias")
        if field_attributes.get(name, Sentinel) == field_attributes["alias"]
    )


def _make_derived_aliases_follow_alias(attributes: dict[str, Any], derived_aliases: frozenset[str]) -> dict[str, Any]:
    """Make the aliases that Field() derived from alias follow it when field().had() or field().didnt_have() change it.

    We pass them explicitly so that they override the ones from the Field() in the annotation of the field, e.g.
    `Annotated[str, Field(alias="a")]`, even as None for `had(alias=None)`. If didnt_have() deleted the alias, they
    are deleted too. Like didnt_have() always did, this keeps an alias from the Field() in the annotation.
    """
    if "alias" in attributes:
        return attributes | dict.fromkeys(derived_aliases, attributes["alias"])
    return {name: value for name, value in attributes.items() if name not in derived_aliases}


def _keep_last_of_default_and_default_factory(
    attributes: dict[str, Any], changes: "list[_FieldAttributeChange]"
) -> dict[str, Any]:
    """Field() accepts only one of them so if the model has both, it keeps the one that it got last.

    The model can have both because the changes of its siblings that deleted one of them do not apply to it.
    """
    if not _has_default_and_default_factory(attributes):
        return attributes
    # Pydantic rejects declarations with both, so a change has set one of them
    last_set = next(
        change.name
        for change in reversed(changes)
        if not change.is_deleted and change.name in {"default", "default_factory"}
    )
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


def _change_field(
    model: "_PydanticModelWrapper[BaseModel]",
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
    model: "_PydanticModelWrapper[BaseModel]",
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


def _delete_field_from_model(model: "_PydanticModelWrapper[BaseModel]", field_name: str, version_change_name: str):
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
