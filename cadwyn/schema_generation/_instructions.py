import collections.abc  # noqa: TC003 # Needed by runtime annotation introspection.
import inspect
from collections.abc import Sequence
from typing import (
    TYPE_CHECKING,
    ClassVar,
    NamedTuple,
    Union,
    assert_never,
    cast,
    get_origin,
)

import pydantic._internal._decorators
from pydantic import BaseModel
from pydantic._internal._config import ConfigWrapper
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from cadwyn._utils import (
    Sentinel,
    get_name_of_function_wrapped_in_pydantic_validator,
    is_same_value,
    lenient_issubclass,
)
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.schemas import (
    AlterSchemaSubInstruction,
    FieldDidntExistInstruction,
    FieldDidntHaveInstruction,
    FieldExistedAsInstruction,
    FieldHadInstruction,
    SchemaHadInstruction,
    ValidatorDidntExistInstruction,
    ValidatorExistedInstruction,
)

from ._annotation_evaluation import (
    _get_annotations_without_evaluating,
)
from ._enums import (
    _apply_alter_enum_instructions,
)
from ._fields import (
    PydanticFieldWrapper,
    _change_field,
    _delete_field_attributes,
    _delete_field_from_model,
    _wrap_validator,
)
from ._generics import (
    _is_generic_model,
    _substitute_type_vars_of_inherited_annotation,
)

if TYPE_CHECKING:
    from ._models import (
        _PydanticModelWrapper,
    )
    from ._schema_generator import (
        SchemaGenerator,
        _RuntimeSchemaGenContext,
    )


class _FieldRename(NamedTuple):
    model: "_PydanticModelWrapper[BaseModel]"
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
    model: "_PydanticModelWrapper[BaseModel]"
    name: str
    version_change_name: str


class _SchemaChanges(NamedTuple):
    renames: list[_FieldRename]
    deleted_fields: list[_DeletedField]


def _migrate_classes(context: "_RuntimeSchemaGenContext") -> _SchemaChanges:
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


def _get_name_of_field(model: "_PydanticModelWrapper[BaseModel]", field_identity: int) -> Union[str, None]:
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
    modified_schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
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
    model: "_PydanticModelWrapper[BaseModel]",
    alter_schema_instruction: SchemaHadInstruction,
    version_change_name: str,
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
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


_VALIDATE_BY_KEYS = frozenset({"validate_by_alias", "validate_by_name"})


def _change_model_config(
    model: "_PydanticModelWrapper[BaseModel]",
    config_patch: pydantic.ConfigDict,
    version_change_name: str,
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
) -> None:
    config = model.get_config(schemas).copy()
    current = ConfigWrapper(config.copy(), check=False)
    # Pydantic <2.14 fills in the validation settings here; newer versions keep an effective config separately.
    current.core_config(None)
    changed_keys = frozenset(config_patch)
    if _VALIDATE_BY_KEYS & changed_keys:
        # Pydantic derives each of these flags from the other one and from populate_by_name,
        # so we set both to keep the effective value of the one that the patch omits
        config.update(validate_by_alias=current.validate_by_alias, validate_by_name=current.validate_by_name)
        changed_keys |= _VALIDATE_BY_KEYS
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
    model.config_changed_keys |= changed_keys


def _get_description(docstring: Union[str, None]) -> Union[str, None]:
    """Return the description that pydantic generates from the docstring of a model"""
    return inspect.cleandoc(docstring) if docstring else None


def _add_field_to_model(
    model: "_PydanticModelWrapper[BaseModel]",
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
    model: "_PydanticModelWrapper[BaseModel]",
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


def _get_subclasses_that_share_field(
    model: "_PydanticModelWrapper[BaseModel]",
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
    model: "_PydanticModelWrapper[BaseModel]",
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
