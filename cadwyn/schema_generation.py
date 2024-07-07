import copy
import dataclasses
from collections.abc import Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, cast, get_args, get_origin

import pydantic
import pydantic._internal._decorators
from pydantic import BaseModel
from typing_extensions import Annotated, assert_never

from cadwyn._utils import Sentinel
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.runtime_compat import (
    PydanticFieldWrapper,
    _EnumWrapper,
    _ModelBundle,
    _PerFieldValidatorWrapper,
    _PydanticRuntimeModelWrapper,
    _SchemaGenerator,
    _wrap_validator,
    is_constrained_type,
    wrap_pydantic_model,
)
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
)

if TYPE_CHECKING:
    from cadwyn.structure.versions import HeadVersion, Version, VersionBundle


@dataclasses.dataclass(slots=True, kw_only=True)
class RuntimeSchemaGenContext:
    version_bundle: "VersionBundle"
    current_version: "Version | HeadVersion"
    models: _ModelBundle
    latest_version: "Version" = dataclasses.field(init=False)

    def __post_init__(self):
        self.latest_version = max(self.version_bundle.versions, key=lambda v: v.value)


def _generate_versioned_models(versions: "VersionBundle") -> "dict[str, _SchemaGenerator]":
    models = _create_model_bundle(versions)

    version_to_context_map = {"head": _SchemaGenerator(copy.deepcopy(models))}
    context = RuntimeSchemaGenContext(current_version=versions.head_version, models=models, version_bundle=versions)
    _migrate_classes(context)

    for version in versions.versions:
        context = RuntimeSchemaGenContext(current_version=version, models=models, version_bundle=versions)
        version_to_context_map[str(version.value)] = _SchemaGenerator(copy.deepcopy(models))
        # note that the last migration will not contain any version changes so we don't need to save the results
        _migrate_classes(context)

    return version_to_context_map


def _create_model_bundle(versions: "VersionBundle"):
    return _ModelBundle(
        enums={enum: _EnumWrapper(enum) for enum in versions.versioned_enums.values()},
        schemas={schema: wrap_pydantic_model(schema) for schema in versions.versioned_schemas.values()},
    )


def _migrate_classes(context: RuntimeSchemaGenContext) -> None:
    for version_change in context.current_version.version_changes:
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
    modified_schemas: dict[type, _PydanticRuntimeModelWrapper],
    alter_schema_instructions: Sequence[AlterSchemaSubInstruction | SchemaHadInstruction],
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
            validator_name = alter_schema_instruction.validator.__name__
            raw_validator = cast(
                pydantic._internal._decorators.PydanticDescriptorProxy, alter_schema_instruction.validator
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
    enums: dict[type, _EnumWrapper],
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
    model: _PydanticRuntimeModelWrapper,
    alter_schema_instruction: SchemaHadInstruction,
    version_change_name: str,
):
    # We only handle names right now so we just go ahead and check
    if alter_schema_instruction.name == model.name:
        raise InvalidGenerationInstructionError(
            f'You tried to change the name of "{model.name}" in "{version_change_name}" '
            "but it already has the name you tried to assign.",
        )

    model.name = alter_schema_instruction.name


def _add_field_to_model(
    model: _PydanticRuntimeModelWrapper,
    schemas: "dict[type, _PydanticRuntimeModelWrapper]",
    alter_schema_instruction: FieldExistedAsInstruction,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to add a field "{alter_schema_instruction.name}" to "{model.name}" '
            f'in "{version_change_name}" but there is already a field with that name.',
        )

    field = PydanticFieldWrapper(alter_schema_instruction.field)
    model.fields[alter_schema_instruction.name] = field
    model.annotations[alter_schema_instruction.name] = alter_schema_instruction.type


def _change_field_in_model(
    model: _PydanticRuntimeModelWrapper,
    schemas: "dict[type, _PydanticRuntimeModelWrapper]",
    alter_schema_instruction: FieldHadInstruction | FieldDidntHaveInstruction,
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
    model: _PydanticRuntimeModelWrapper,
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
    annotation: Any | None,
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
            if annotation is not None and hasattr(annotation, attr_name):
                _setattr_on_constrained_type(annotation, attr_name, attr_value)
            else:
                field.update_attribute(name=attr_name, value=attr_value)


def _setattr_on_constrained_type(constrained_type_annotation: Any, attr_name: str, attr_value: Any) -> None:
    object.__setattr__(constrained_type_annotation, attr_name, attr_value)


def _delete_field_attributes(
    model: _PydanticRuntimeModelWrapper,
    alter_schema_instruction: FieldDidntHaveInstruction,
    version_change_name: str,
    field: PydanticFieldWrapper,
    annotation: Any,
) -> None:
    for attr_name in alter_schema_instruction.attributes:
        if attr_name in field.passed_field_attributes:
            field.delete_attribute(name=attr_name)
            continue
        elif get_origin(annotation) == Annotated and any(
            hasattr(sub_ann, attr_name) for sub_ann in get_args(annotation)
        ):
            for sub_ann in get_args(annotation):
                if hasattr(sub_ann, attr_name):
                    _setattr_on_constrained_type(sub_ann, attr_name, None)
            continue
        else:
            raise InvalidGenerationInstructionError(
                f'You tried to delete the attribute "{attr_name}" of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already doesn't have that attribute.",
            )


def _delete_field_from_model(model: _PydanticRuntimeModelWrapper, field_name: str, version_change_name: str):
    if field_name not in model.fields:
        raise InvalidGenerationInstructionError(
            f'You tried to delete a field "{field_name}" from "{model.name}" '
            f'in "{version_change_name}" but it doesn\'t have such a field.',
        )
    model.fields.pop(field_name)
    model.annotations.pop(field_name)
    for validator_name, validator in model.validators.copy().items():
        if isinstance(validator, _PerFieldValidatorWrapper) and field_name in validator.fields:
            validator.fields.remove(field_name)
            # TODO: This behavior doesn't feel natural
            if not validator.fields:
                model.validators[validator_name].is_deleted = True
