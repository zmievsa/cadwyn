import ast
import dataclasses
from collections.abc import Callable, Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, cast, get_args

from issubclass import issubclass
from pydantic import BaseModel
from typing_extensions import Self, assert_never

from cadwyn._asts import get_fancy_repr
from cadwyn._compat import (
    PYDANTIC_V2,
    FieldInfo,
    PydanticFieldWrapper,
    dict_of_empty_field_info,
    is_constrained_type,
)
from cadwyn._utils import Sentinel
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

if TYPE_CHECKING:
    from cadwyn.codegen._common import _FieldName
    from cadwyn.structure.versions import HeadVersion, Version, VersionBundle

_T_MODEL = TypeVar("_T_MODEL", bound=type[BaseModel | Enum])


@dataclasses.dataclass(slots=True)
class _EnumWrapper:
    cls: type[Enum]
    members: "dict[_FieldName, Enum | object]"


@dataclasses.dataclass(slots=True)
class _ValidatorWrapper:
    validator: Callable
    is_deleted: bool = False


@dataclasses.dataclass(slots=True)
class _PydanticRuntimeModelWrapper:
    cls: type[BaseModel]
    name: str
    fields: "dict[_FieldName, PydanticFieldWrapper]"
    validators: "dict[_FieldName, _ValidatorWrapper]"
    annotations: dict[str, Any] = dataclasses.field(init=False, repr=False)
    _parents: list[Self] | None = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        self.annotations = self.cls.__annotations__.copy()

    def _get_parents(self, schemas: "dict[type, Self]"):
        if self._parents is not None:
            return self._parents
        parents = []
        for base in self.cls.mro()[1:]:
            if base in schemas:
                parents.append(schemas[base])
            elif issubclass(base, BaseModel):
                fields, validators = get_fields_and_validators_from_model(base)
                parents.append(type(self)(base, base.__name__, fields, validators))
        self._parents = parents
        return parents

    def _get_defined_fields_through_mro(self, schemas: "dict[type, Self]") -> dict[str, PydanticFieldWrapper]:
        fields = {}

        for parent in reversed(self._get_parents(schemas)):
            fields |= parent.fields

        return fields | self.fields

    def _get_defined_annotations_through_mro(self, schemas: "dict[type, Self]") -> dict[str, Any]:
        annotations = {}

        for parent in reversed(self._get_parents(schemas)):
            annotations |= parent.annotations

        return annotations | self.annotations


@dataclasses.dataclass(slots=True)
class _ModelBundle:
    enums: dict[type[Enum], _EnumWrapper]
    schemas: dict[type[BaseModel], _PydanticRuntimeModelWrapper]


@dataclasses.dataclass(slots=True, kw_only=True)
class RuntimeSchemaGenContext:
    version_bundle: "VersionBundle"
    current_version: "Version | HeadVersion"
    models: _ModelBundle
    latest_version: "Version" = dataclasses.field(init=False)

    def __post_init__(self):
        self.latest_version = max(self.version_bundle.versions, key=lambda v: v.value)


def _generate_versioned_models(versions: "VersionBundle") -> "dict[str, dict[_T_MODEL, _T_MODEL]]":
    models = _create_model_bundle(versions)

    version_to_context_map = {"head": _copy_classes(models.schemas, models.enums)}
    context = RuntimeSchemaGenContext(current_version=versions.head_version, models=models, version_bundle=versions)
    _migrate_classes(context)

    for version in versions.versions:
        context = RuntimeSchemaGenContext(current_version=version, models=models, version_bundle=versions)
        version_to_context_map[str(version.value)] = _copy_classes(models.schemas, models.enums)
        # note that the last migration will not contain any version changes so we don't need to save the results
        _migrate_classes(context)

    return version_to_context_map


def _create_model_bundle(versions: "VersionBundle"):
    schemas = {}
    for schema in versions.versioned_schemas:
        breakpoint()
        fields, validators = None, None
        schemas[schema] = _PydanticRuntimeModelWrapper(schema, schema.__name__, fields, validators)
    return _ModelBundle(
        enums={
            enum: _EnumWrapper(enum, {member.name: member.value for member in enum})
            for enum in versions.versioned_enums.values()
        },
        schemas=schemas,
    )


def _migrate_classes(context: RuntimeSchemaGenContext) -> None:
    for version_change in context.current_version.version_changes:
        # _apply_alter_schema_instructions(
        #     context.models.schemas,
        #     version_change.alter_schema_instructions,
        #     version_change.__name__,
        # )
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
            schema_info.validators[validator_name] = _ValidatorWrapper(
                alter_schema_instruction.validator,
                alter_schema_instruction.validator_info.is_deleted,
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

    fancy_type_repr = get_fancy_repr(alter_schema_instruction.type)
    field = PydanticFieldWrapper(
        annotation_ast=ast.parse(fancy_type_repr, mode="eval").body,
        annotation=alter_schema_instruction.type,
        init_model_field=alter_schema_instruction.field,
        value_ast=None,
    )
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

    constrained_type_annotation = None

    # This is only gonna be true if the field is Annotated
    if annotated_first_arg_ast is not None:
        # PydanticV2 changed field annotation handling so field.annotation lies to us
        real_annotation = model._get_defined_annotations_through_mro(schemas)[alter_schema_instruction.name]
        type_annotation = get_args(real_annotation)[0]
        if is_constrained_type(type_annotation):
            constrained_type_annotation = type_annotation
    else:
        type_annotation = field.annotation
        if is_constrained_type(type_annotation):
            constrained_type_annotation = type_annotation
    if isinstance(alter_schema_instruction, FieldHadInstruction):
        # TODO: This naming sucks
        _change_field(
            model,
            alter_schema_instruction,
            version_change_name,
            defined_annotations,
            field,
            constrained_type_annotation,
        )
    else:
        _delete_field_attributes(
            model,
            alter_schema_instruction,
            version_change_name,
            field,
            constrained_type_annotation,
        )


def _change_field(
    model: _PydanticRuntimeModelWrapper,
    alter_schema_instruction: FieldHadInstruction,
    version_change_name: str,
    defined_annotations: dict[str, Any],
    field: PydanticFieldWrapper,
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
                if not PYDANTIC_V2 and not type_is_call:  # pragma: no branch
                    field.update_attribute(name=attr_name, value=attr_value)
            else:
                field.update_attribute(name=attr_name, value=attr_value)


def _setattr_on_constrained_type(constrained_type_annotation: Any, attr_name: str, attr_value: Any) -> None:
    setattr(constrained_type_annotation, attr_name, attr_value)


def _delete_field_attributes(
    model: _PydanticRuntimeModelWrapper,
    alter_schema_instruction: FieldDidntHaveInstruction,
    version_change_name: str,
    field: PydanticFieldWrapper,
    constrained_type_annotation: Any,
) -> None:
    for attr_name in alter_schema_instruction.attributes:
        if attr_name in field.passed_field_attributes:
            field.delete_attribute(name=attr_name)
        # In case annotation_ast is a conint/constr/etc. Notice how we do not support
        # the same operation for **adding** constraints for simplicity.
        elif (hasattr(constrained_type_annotation, attr_name)) or contype_is_definitely_used:
            if hasattr(constrained_type_annotation, attr_name):
                _setattr_on_constrained_type(constrained_type_annotation, attr_name, None)
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


def _copy_classes(schemas: dict[type[BaseModel], _PydanticRuntimeModelWrapper], enums: dict[type[Enum], _EnumWrapper]):
    return {k: _copy_enum(wrapper.cls, wrapper.members) for k, wrapper in enums.items()} | {
        k: wrapper.cls for k, wrapper in schemas.items()
    }


class _DummyEnum(Enum):
    pass


def _get_initialization_namespace_for_enum(enum_cls: type[Enum]):
    mro_without_the_class_itself = enum_cls.mro()[1:]

    mro_dict = {}
    for cls in reversed(mro_without_the_class_itself):
        mro_dict.update(cls.__dict__)

    methods = {
        k: v
        for k, v in enum_cls.__dict__.items()
        if k not in enum_cls._member_names_
        and k not in _DummyEnum.__dict__
        and (k not in mro_dict or mro_dict[k] is not v)
    }
    return methods


def _copy_enum(enum_cls: type[Enum], member_map: dict[str, Any]):
    enum_dict = Enum.__prepare__(enum_cls.__name__, enum_cls.__bases__)
    raw_member_map = {k: v.value if isinstance(v, Enum) else v for k, v in member_map.items()}
    initialization_namespace = _get_initialization_namespace_for_enum(enum_cls) | raw_member_map
    for attr_name, attr in initialization_namespace.items():
        enum_dict[attr_name] = attr
    return type(enum_cls.__name__, enum_cls.__bases__, enum_dict)
