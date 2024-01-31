import ast
from collections.abc import Sequence
from typing import Annotated, Any, cast, get_args, get_origin

from typing_extensions import assert_never

from cadwyn._compat import (
    PYDANTIC_V2,
    FieldInfo,
    PydanticFieldWrapper,
    dict_of_empty_field_info,
    is_constrained_type,
)
from cadwyn._package_utils import IdentifierPythonPath, get_cls_pythonpath
from cadwyn._utils import Sentinel
from cadwyn.codegen._asts import add_keyword_to_call, delete_keyword_from_call, get_fancy_repr
from cadwyn.codegen._common import GlobalCodegenContext, PydanticModelWrapper, _EnumWrapper
from cadwyn.exceptions import InvalidGenerationInstructionError
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
    alter_schema_instructions: Sequence[AlterSchemaSubInstruction | SchemaHadInstruction],
    version_change_name: str,
):
    for alter_schema_instruction in alter_schema_instructions:
        schema = alter_schema_instruction.schema
        schema_path = get_cls_pythonpath(schema)
        schema_info = modified_schemas[schema_path]
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
            schema_info.validators[validator_name] = alter_schema_instruction.validator_info
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
    model: PydanticModelWrapper,
    schemas: "dict[IdentifierPythonPath, PydanticModelWrapper]",
    alter_schema_instruction: FieldExistedAsInstruction,
    version_change_name: str,
):
    defined_fields = model._get_defined_fields_through_mro(schemas)
    if alter_schema_instruction.name in defined_fields:
        raise InvalidGenerationInstructionError(
            f'You tried to add a field "{alter_schema_instruction.name}" to "{model.name}" '
            f'in "{version_change_name}" but there is already a field with that name.',
        )

    fancy_type_repr = get_fancy_repr(alter_schema_instruction.type)
    field = PydanticFieldWrapper(
        annotation_ast=ast.parse(fancy_type_repr, mode="eval").body,
        annotation=alter_schema_instruction.type,
        init_model_field=alter_schema_instruction.field,
        value_ast=None,
    )
    model.fields[alter_schema_instruction.name] = field

    passed_field_attributes = field.passed_field_attributes
    if passed_field_attributes:
        field_call_ast = cast(ast.Call, ast.parse("Field()", mode="eval").body)
        for attr_name, attr_value in passed_field_attributes.items():
            add_keyword_to_call(attr_name, attr_value, field_call_ast)
        field.value_ast = field_call_ast
    model.cls.__annotations__[alter_schema_instruction.name] = alter_schema_instruction.type


def _change_field_in_model(
    model: PydanticModelWrapper,
    schemas: "dict[IdentifierPythonPath, PydanticModelWrapper]",
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
    model.cls.__annotations__[alter_schema_instruction.name] = defined_annotations[alter_schema_instruction.name]

    annotation_ast, field_call_ast, contype_is_definitely_used = _get_constraint_asts_and_field_call_ast(
        schemas, model, alter_schema_instruction.name, field
    )

    constrained_type_annotation = None

    if annotation_ast is not None:
        field_type_is_annotated = True  # I.e. typing.Annotated
        # PydanticV2 changed field annotation handling so field.annotation lies to us
        real_annotation = model._get_defined_annotations_through_mro(schemas)[alter_schema_instruction.name]
        type_annotation = get_args(real_annotation)[0]
        if is_constrained_type(type_annotation):
            constrained_type_annotation = type_annotation
    else:
        field_type_is_annotated = False
        type_annotation = field.annotation
        if is_constrained_type(type_annotation):
            constrained_type_annotation = type_annotation
        annotation_ast = field.annotation_ast
    if field_call_ast is None:
        field_call_ast = field.value_ast

    if isinstance(alter_schema_instruction, FieldHadInstruction):
        # TODO: This naming sucks
        _change_field(
            model,
            alter_schema_instruction,
            version_change_name,
            defined_annotations,
            field,
            annotation_ast,
            field_call_ast,
            type_annotation,
            field_type_is_annotated,
            constrained_type_annotation,
        )
    else:
        _delete_field_attributes(
            model,
            alter_schema_instruction,
            version_change_name,
            field,
            annotation_ast,
            field_call_ast,
            type_annotation,
            constrained_type_annotation,
            contype_is_definitely_used,
        )


def _change_field(  # noqa: C901
    model: PydanticModelWrapper,
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
    annotation_ast: ast.expr | None,
    field_call_ast: ast.expr | None,
    type_annotation: Any,
    field_type_is_annotated: bool,
    constrained_type_annotation: Any | None,
):
    if alter_schema_instruction.type is not Sentinel:
        if field.annotation == alter_schema_instruction.type:
            raise InvalidGenerationInstructionError(
                f'You tried to change the type of field "{alter_schema_instruction.name}" to '
                f'"{alter_schema_instruction.type}" from "{model.name}" in "{version_change_name}" '
                f'but it already has type "{field.annotation}"',
            )
        field.annotation = alter_schema_instruction.type
        model.cls.__annotations__[alter_schema_instruction.name] = alter_schema_instruction.type
        fancy_type_repr = get_fancy_repr(alter_schema_instruction.type)
        field.annotation_ast = ast.parse(fancy_type_repr, mode="eval").body

    if alter_schema_instruction.new_name is not Sentinel:
        if alter_schema_instruction.new_name == alter_schema_instruction.name:
            raise InvalidGenerationInstructionError(
                f'You tried to change the name of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already has that name.",
            )
        model.fields[alter_schema_instruction.new_name] = model.fields.pop(alter_schema_instruction.name)
        model.cls.__annotations__[alter_schema_instruction.new_name] = model.cls.__annotations__.pop(
            alter_schema_instruction.name,
            defined_annotations[alter_schema_instruction.name],
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
                    f'"{alter_schema_instruction.name}" '
                    f'from "{model.name}" to {attr_value!r} in "{version_change_name}" '
                    "but it already has that value.",
                )
            if constrained_type_annotation is not None and hasattr(constrained_type_annotation, attr_name):
                _setattr_on_constrained_type(constrained_type_annotation, attr_name, attr_value)
                if isinstance(annotation_ast, ast.Call):
                    add_keyword_to_call(attr_name, attr_value, annotation_ast)
                elif not PYDANTIC_V2:  # pragma: no branch
                    field.update_attribute(name=attr_name, value=attr_value)
                    if isinstance(field_call_ast, ast.Call):  # pragma: no branch
                        add_keyword_to_call(attr_name, attr_value, field_call_ast)

            else:
                field.update_attribute(name=attr_name, value=attr_value)
                if field_type_is_annotated and attr_name == "default":
                    field.value_ast = ast.parse(get_fancy_repr(attr_value), mode="eval").body
                elif isinstance(field_call_ast, ast.Call):
                    add_keyword_to_call(attr_name, attr_value, field_call_ast)
                elif field.value_ast is not None:
                    field_call_ast = cast(ast.Call, ast.parse("Field()", mode="eval").body)
                    add_keyword_to_call("default", field.value_ast, field_call_ast)
                    add_keyword_to_call(attr_name, attr_value, field_call_ast)
                    field.value_ast = field_call_ast
                else:
                    field.value_ast = cast(ast.Call, ast.parse("Field()", mode="eval").body)
                    field_call_ast = field.value_ast
                    add_keyword_to_call(attr_name, attr_value, field_call_ast)


def _setattr_on_constrained_type(constrained_type_annotation: Any, attr_name: str, attr_value: Any) -> None:
    setattr(constrained_type_annotation, attr_name, attr_value)


def _delete_field_attributes(
    model: PydanticModelWrapper,
    alter_schema_instruction: FieldDidntHaveInstruction,
    version_change_name: str,
    field: PydanticFieldWrapper,
    type_annotation_ast: ast.expr | None,
    field_call_ast: ast.expr | None,
    type_annotation: Any,
    constrained_type_annotation: Any,
    contype_is_definitely_used: bool,
) -> None:
    for attr_name in alter_schema_instruction.attributes:
        if attr_name in field.passed_field_attributes:
            field.delete_attribute(name=attr_name)
            if isinstance(field_call_ast, ast.Call):
                delete_keyword_from_call(attr_name, field_call_ast)
            elif attr_name == "default":  # pragma: no branch
                field.value_ast = None
        # In case annotation_ast is a conint/constr/etc. Notice how we do not support
        # the same operation for **adding** constraints for simplicity.
        elif (hasattr(constrained_type_annotation, attr_name)) or contype_is_definitely_used:
            if hasattr(constrained_type_annotation, attr_name):
                _setattr_on_constrained_type(constrained_type_annotation, attr_name, None)
            if isinstance(type_annotation_ast, ast.Call):  # pragma: no branch
                delete_keyword_from_call(attr_name, type_annotation_ast)
        else:
            raise InvalidGenerationInstructionError(
                f'You tried to delete the attribute "{attr_name}" of field "{alter_schema_instruction.name}" '
                f'from "{model.name}" in "{version_change_name}" '
                "but it already doesn't have that attribute.",
            )


ContypeIsDefinitelyUsed = bool
CONTYPES = (
    "conbytes",
    "condate",
    "condecimal",
    "confloat",
    "conint",
    "conlist",
    "conset",
    "constr",
)


def _get_constraint_asts_and_field_call_ast(
    schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
    model: PydanticModelWrapper,
    field_name: str,
    field: PydanticFieldWrapper,
) -> tuple[ast.expr | None, ast.Call | None, ContypeIsDefinitelyUsed]:
    """If the field type is Annotated and contains "Field" """
    # We return both annotation ast and field call ast because annotation might be a constrained type such as conint
    # and therefore contain constraints that we might want to remove.

    # ContypeIsDefinitely used is used to determine whether constr/conint/etc is used in Pydantic 2
    # because pydantic 2 changes original type hints to make sure that conint/constr/etc do not appear in annotations.

    real_annotation = model._get_defined_annotations_through_mro(schemas)[field_name]
    # typing.Annotated scenario
    if get_origin(real_annotation) == Annotated:
        index_of_field_info = _find_index_of_field_info_in_annotated(real_annotation)
        if not (isinstance(field.annotation_ast, ast.Subscript) and isinstance(field.annotation_ast.slice, ast.Tuple)):
            return (field.annotation_ast, None, True)

        unparsed_annotation = ast.unparse(field.annotation_ast.slice.elts[0])
        contype_is_definitely_used = any(contype in unparsed_annotation for contype in CONTYPES)

        # In pydantic 2, this means that in fact there is conint/constr/etc instead of an actual Annotated.
        # Yes, pydantic 2 changes original type hints to make sure that conint/constr/etc do not appear in types.

        if index_of_field_info is not None:
            field_call_ast = field.annotation_ast.slice.elts[index_of_field_info]

            return (field.annotation_ast.slice.elts[0], cast(ast.Call, field_call_ast), contype_is_definitely_used)
        return (field.annotation_ast.slice.elts[0], None, contype_is_definitely_used)
    return (None, None, False)


def _find_index_of_field_info_in_annotated(real_annotation: Any):
    # Pydantic turns `Annotated[conint(lt=2 + 5),                Field(default=11),     annotated_types.Gt(0)]` into:
    #                `Annotated[int, None, Interval(lt=7), None, FieldInfo(default=11), annotated_types.Gt(0)]`
    # Why? No idea. Probably due to its weird handling of constrained types. So we gotta go from the last element
    # because constrained types can only appear and mess up indexing in the first element.
    for i, arg in enumerate(reversed(get_args(real_annotation)), start=1):
        if isinstance(arg, FieldInfo):
            return -i
    return None


def _delete_field_from_model(model: PydanticModelWrapper, field_name: str, version_change_name: str):
    if field_name not in model.fields:
        raise InvalidGenerationInstructionError(
            f'You tried to delete a field "{field_name}" from "{model.name}" '
            f'in "{version_change_name}" but it doesn\'t have such a field.',
        )
    model.fields.pop(field_name)
    for validator_name, validator in model.validators.copy().items():
        if validator.field_names is not None and field_name in validator.field_names:
            validator.field_names.remove(field_name)

            validator_decorator = cast(
                ast.Call, validator.func_ast.decorator_list[validator.index_of_validator_decorator]
            )
            for arg in validator_decorator.args.copy():
                if isinstance(arg, ast.Constant) and arg.value == field_name:
                    validator_decorator.args.remove(arg)
            validator.func_ast.decorator_list[0]
            if not validator.field_names:
                model.validators[validator_name].is_deleted = True
