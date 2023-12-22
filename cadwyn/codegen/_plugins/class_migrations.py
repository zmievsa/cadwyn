import ast
from collections.abc import Sequence

from typing_extensions import assert_never

from cadwyn._compat import FieldInfo, PydanticFieldWrapper, dict_of_empty_field_info, is_pydantic_constrained_type
from cadwyn._package_utils import IdentifierPythonPath, get_cls_pythonpath
from cadwyn._utils import Sentinel
from cadwyn.codegen._asts import add_keyword_to_call
from cadwyn.codegen._common import GlobalCodegenContext, PydanticModelWrapper, _EnumWrapper
from cadwyn.exceptions import InvalidGenerationInstructionError
from cadwyn.structure.enums import AlterEnumSubInstruction, EnumDidntHaveMembersInstruction, EnumHadMembersInstruction
from cadwyn.structure.schemas import (
    AlterSchemaInstruction,
    AlterSchemaSubInstruction,
    OldSchemaFieldDidntExist,
    OldSchemaFieldExistedWith,
    OldSchemaFieldHad,
)


def class_migration_plugin(context: GlobalCodegenContext):
    for version_change in context.current_version.version_changes:
        _apply_alter_schema_instructions(
            context.schemas,
            version_change.alter_schema_instructions,
            version_change.__name__,
        )
        _apply_alter_enum_instructions(
            context.enums,
            version_change.alter_enum_instructions,
            version_change.__name__,
        )


def _apply_alter_schema_instructions(
    modified_schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
    alter_schema_instructions: Sequence[AlterSchemaSubInstruction | AlterSchemaInstruction],
    version_change_name: str,
):
    for alter_schema_instruction in alter_schema_instructions:
        schema = alter_schema_instruction.schema
        schema_path = get_cls_pythonpath(schema)
        mutable_schema_info = modified_schemas[schema_path]
        if isinstance(alter_schema_instruction, OldSchemaFieldDidntExist):
            _delete_field_from_model(mutable_schema_info, alter_schema_instruction.field_name, version_change_name)
        elif isinstance(alter_schema_instruction, OldSchemaFieldHad):
            _change_field_in_model(
                mutable_schema_info,
                modified_schemas,
                alter_schema_instruction,
                version_change_name,
            )
        elif isinstance(alter_schema_instruction, OldSchemaFieldExistedWith):
            _add_field_to_model(mutable_schema_info, modified_schemas, alter_schema_instruction, version_change_name)
        elif isinstance(alter_schema_instruction, AlterSchemaInstruction):
            _change_model(mutable_schema_info, alter_schema_instruction, version_change_name)
        else:
            assert_never(alter_schema_instruction)


def _apply_alter_enum_instructions(
    enums: dict[IdentifierPythonPath, _EnumWrapper],
    alter_enum_instructions: Sequence[AlterEnumSubInstruction],
    version_change_name: str,
):
    for alter_enum_instruction in alter_enum_instructions:
        enum = alter_enum_instruction.enum
        enum_path = get_cls_pythonpath(enum)
        enum = enums[enum_path]
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
    model: PydanticModelWrapper,
    alter_schema_instruction: AlterSchemaInstruction,
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
    model: PydanticModelWrapper,
    schemas: "dict[IdentifierPythonPath, PydanticModelWrapper]",
    alter_schema_instruction: OldSchemaFieldExistedWith,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields(schemas)
    if alter_schema_instruction.field_name in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to add a field "{alter_schema_instruction.field_name}" to "{model.name}" '
            f'in "{version_change_name}" but there is already a field with that name.',
        )

    model.fields[alter_schema_instruction.field_name] = PydanticFieldWrapper(
        annotation_ast=None,
        annotation=alter_schema_instruction.type,
        init_model_field=alter_schema_instruction.field,
        field_ast=None,
    )


def _change_field_in_model(
    model: PydanticModelWrapper,
    schemas: "dict[IdentifierPythonPath, PydanticModelWrapper]",
    alter_schema_instruction: OldSchemaFieldHad,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields(schemas)
    if alter_schema_instruction.field_name not in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to change the type of field "{alter_schema_instruction.field_name}" from '
            f'"{model.name}" in "{version_change_name}" but it doesn\'t have such a field.',
        )

    field = defined_fields[alter_schema_instruction.field_name]
    model.fields[alter_schema_instruction.field_name] = field

    current_field_is_constrained_type = is_pydantic_constrained_type(field.annotation)
    if alter_schema_instruction.type is not Sentinel:
        if field.annotation == alter_schema_instruction.type:
            raise InvalidGenerationInstructionError(
                f'You tried to change the type of field "{alter_schema_instruction.field_name}" to '
                f'"{alter_schema_instruction.type}" from "{model.name}" in "{version_change_name}" '
                f'but it already has type "{field.annotation}"',
            )
        field.annotation = alter_schema_instruction.type

        field.annotation_ast = None
        if current_field_is_constrained_type:
            field.field_ast = None

    if alter_schema_instruction.new_name is not Sentinel:
        if alter_schema_instruction.new_name == alter_schema_instruction.field_name:
            raise InvalidGenerationInstructionError(
                f'You tried to change the name of field "{alter_schema_instruction.field_name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already has that name.",
            )
        model.fields[alter_schema_instruction.new_name] = model.fields.pop(
            alter_schema_instruction.field_name,
        )

    field_info = field.field_info

    dict_of_field_info = {k: getattr(field_info, k) for k in field_info.__slots__}
    if dict_of_field_info == dict_of_empty_field_info:
        field_info = FieldInfo()
        field.field_info = field_info
    for attr_name in alter_schema_instruction.field_changes.__dataclass_fields__:
        attr_value = getattr(alter_schema_instruction.field_changes, attr_name)
        if attr_value is not Sentinel:
            if field.passed_field_attributes.get(attr_name, Sentinel) == attr_value:
                raise InvalidGenerationInstructionError(
                    f'You tried to change the attribute "{attr_name}" of field '
                    f'"{alter_schema_instruction.field_name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )

            if hasattr(field.annotation, attr_name) and current_field_is_constrained_type:
                setattr(field.annotation, attr_name, attr_value)
                ann_ast = field.annotation_ast
                if ann_ast is not None and isinstance(ann_ast, ast.Call):
                    add_keyword_to_call(attr_name, attr_value, ann_ast)
                else:
                    field.field_ast = None
                    field.annotation_ast = None
            else:
                field.update_attribute(name=attr_name, value=attr_value)
                field_ast = field.field_ast
                if isinstance(field_ast, ast.Call):
                    add_keyword_to_call(attr_name, attr_value, field_ast)
                else:
                    field.field_ast = None


def _delete_field_from_model(model: PydanticModelWrapper, field_name: str, version_change_name: str):
    if field_name not in model.fields:
        raise InvalidGenerationInstructionError(
            f'You tried to delete a field "{field_name}" from "{model.name}" '
            f'in "{version_change_name}" but it doesn\'t have such a field.',
        )
    model.fields.pop(field_name)
