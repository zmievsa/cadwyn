# from universi.exceptions import LintingError
# from universi.structure.common import Sentinel
# from universi.structure.schemas import (
#     AlterSchemaInstruction,
#     OldSchemaDidntHaveField,
#     OldSchemaFieldWas,
#     OldSchemaHadField,
# )
# from universi.structure.versions import AbstractVersionChange, Versions


# def check_old_schema_field_was(
#     change: OldSchemaFieldWas,
#     alter_schema_instruction: AlterSchemaInstruction,
# ):
#     actual_field_in_old_schema = alter_schema_instruction.old_model.__fields__[
#         change.field_name
#     ]
#     for attr_name, field in change.field_changes.__dict__.items():
#         if field is not Sentinel:
#             expected_old_value = getattr(
#                 actual_field_in_old_schema.field_info,
#                 attr_name,
#             )
#             if expected_old_value != field:
#                 raise LintingError(
#                     f"Expected value {expected_old_value} doesn't match the field value {field} for attribute {attr_name} in model {alter_schema_instruction.old_model}",
#                 )
#     if change.type is not Sentinel and (
#         alter_schema_instruction.old_model.__annotations__[change.field_name]
#         != change.type
#     ):
#         raise LintingError(
#             f"Field type mismatch for field {change.field_name} in model {alter_schema_instruction.old_model}",
#         )


# def check_old_schema_had_field(change: OldSchemaHadField, alter_schema_instruction):
#     if change.field_name not in alter_schema_instruction.old_model.__fields__:
#         raise LintingError(
#             f"Field {change.field_name} not found in old model {alter_schema_instruction.old_model}",
#         )
#     actual_field_in_old_schema = alter_schema_instruction.old_model.__fields__[
#         change.field_name
#     ]
#     for attr_name in actual_field_in_old_schema.field_info.__slots__:
#         expected_old_value = getattr(change.field, attr_name)
#         actual_old_value = getattr(actual_field_in_old_schema.field_info, attr_name)
#         if expected_old_value != actual_old_value:
#             raise LintingError(
#                 f"Attr {attr_name} is not equal in the migration ({expected_old_value}) and in actual old schema ({actual_old_value})",
#             )
#     if change.field_name in alter_schema_instruction.model.__fields__:
#         raise LintingError(
#             f"Unexpected field {change.field_name} found in new model {alter_schema_instruction.model}",
#         )


# def check_old_schema_didnt_have_field(
#     change: OldSchemaDidntHaveField,
#     alter_schema_instruction: AlterSchemaInstruction,
# ):
#     if change.field_name not in alter_schema_instruction.schema.__fields__:
#         raise LintingError(
#             f"Field {change.field_name} missing in new model {alter_schema_instruction.schema}",
#         )
#     if change.field_name in alter_schema_instruction.old_model.__fields__:
#         raise LintingError(
#             f"Unexpected field {change.field_name} found in old model {alter_schema_instruction.old_model}",
#         )


# def check_equivalent_fields(
#     alter_schema_instruction: AlterSchemaInstruction,
#     equivalent_fields,
# ):
#     for field_name in equivalent_fields:
#         old_model_field = alter_schema_instruction.old_model.__fields__[field_name]
#         new_model_field = alter_schema_instruction.schema.__fields__[field_name]

#         if old_model_field.type_ != new_model_field.type_:
#             raise LintingError(
#                 f"Field {field_name} is not equivalent by type in {alter_schema_instruction.old_model} and {alter_schema_instruction.schema}",
#             )

#         for attr_name in old_model_field.field_info.__slots__:
#             old_model_attr = getattr(old_model_field.field_info, attr_name)
#             new_model_attr = getattr(new_model_field.field_info, attr_name)
#             if old_model_attr != new_model_attr:
#                 raise LintingError(
#                     f"Field {field_name} is not equivalent by {attr_name} in {alter_schema_instruction.old_model} and {alter_schema_instruction.schema}",
#                 )


# def check_alter_schema_instruction(alter_schema_instruction: AlterSchemaInstruction):
#     for change in alter_schema_instruction.changes:
#         if isinstance(change, OldSchemaFieldWas):
#             check_old_schema_field_was(change, alter_schema_instruction)
#         elif isinstance(change, OldSchemaHadField):
#             check_old_schema_had_field(change, alter_schema_instruction)
#         elif isinstance(change, OldSchemaDidntHaveField):
#             check_old_schema_didnt_have_field(change, alter_schema_instruction)
#         else:
#             raise LintingError(
#                 f"Unexpected change of type {type(change)} in model {alter_schema_instruction.old_model}",
#             )

#     altered_fields = {change.field_name for change in alter_schema_instruction.changes}
#     equivalent_fields = (
#         set(alter_schema_instruction.old_model.__fields__) - altered_fields
#     )
#     fields_that_are_in_only_one_of_the_models = (
#         alter_schema_instruction.old_model.__fields__.keys()
#         ^ alter_schema_instruction.schema.__fields__.keys()
#     )

#     fields_that_are_not_altered_but_are_in_only_one_of_the_models = (
#         fields_that_are_in_only_one_of_the_models - altered_fields
#     )
#     if fields_that_are_not_altered_but_are_in_only_one_of_the_models:
#         raise LintingError(
#             f"Fields {fields_that_are_not_altered_but_are_in_only_one_of_the_models} are not altered but are in only one of the models: {alter_schema_instruction.old_model} and {alter_schema_instruction.schema}",
#         )

#     check_equivalent_fields(alter_schema_instruction, equivalent_fields)


# def check_version_change(version_change: type[AbstractVersionChange]):
#     for alter_schema_instruction in version_change.alter_schema_instructions:
#         check_alter_schema_instruction(alter_schema_instruction)


# def lint_all_schemas(versions: Versions):
#     for version in versions.versions:
#         for version_change in version.version_changes:
#             check_version_change(version_change)
