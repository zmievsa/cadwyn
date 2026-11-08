"""`cadwyn/structure/versions.py` and `cadwyn/schema_generation.py` were split into packages of private submodules.

Their objects must still report the old modules in error messages, reprs, pickles, and documentation tools.
"""

import enum
import functools
import importlib
import pickle
import re
from collections.abc import Iterator
from types import FunctionType, ModuleType
from typing import Any, ParamSpec, TypeVar, Union

import pytest

from cadwyn._utils import classproperty

_SPLIT_MODULES = ("cadwyn.schema_generation", "cadwyn.structure.versions")
_PRIVATE_SUBMODULE_OF_SPLIT_MODULE = re.compile(r"^cadwyn\.(schema_generation|structure\.versions)\._")

# `__module__` and `__qualname__` of the objects defined in the split modules, and of everything that
# `cadwyn` and `cadwyn.structure` exported, generated from commit 63e8609^, the last one before the splits.
# Cadwyn's code didn't change between that commit and the split of `cadwyn/structure/versions.py`.
_PRE_SPLIT_METADATA: dict[str, dict[str, tuple[str, Union[str, None]]]] = {
    "cadwyn": {
        "Cadwyn": ("cadwyn.applications", "Cadwyn"),
        "HeadVersion": ("cadwyn.structure.versions", "HeadVersion"),
        "RequestInfo": ("cadwyn.structure.data", "RequestInfo"),
        "ResponseInfo": ("cadwyn.structure.data", "ResponseInfo"),
        "Version": ("cadwyn.structure.versions", "Version"),
        "VersionBundle": ("cadwyn.structure.versions", "VersionBundle"),
        "VersionChange": ("cadwyn.structure.versions", "VersionChange"),
        "VersionChangeWithSideEffects": ("cadwyn.structure.versions", "VersionChangeWithSideEffects"),
        "VersionedAPIRouter": ("cadwyn.route_generation", "VersionedAPIRouter"),
        "convert_request_to_next_version_for": ("cadwyn.structure.data", "convert_request_to_next_version_for"),
        "convert_response_to_previous_version_for": (
            "cadwyn.structure.data",
            "convert_response_to_previous_version_for",
        ),
        "current_dependency_solver": ("cadwyn.dependencies", "current_dependency_solver"),
        "endpoint": ("cadwyn.structure.endpoints", "endpoint"),
        "enum": ("cadwyn.structure.enums", "enum"),
        "generate_versioned_models": ("cadwyn.schema_generation", "generate_versioned_models"),
        "generate_versioned_routers": ("cadwyn.route_generation", "generate_versioned_routers"),
        "migrate_response_body": ("cadwyn.schema_generation", "migrate_response_body"),
        "schema": ("cadwyn.structure.schemas", "schema"),
    },
    "cadwyn.structure": {
        "HeadVersion": ("cadwyn.structure.versions", "HeadVersion"),
        "RequestInfo": ("cadwyn.structure.data", "RequestInfo"),
        "ResponseInfo": ("cadwyn.structure.data", "ResponseInfo"),
        "Version": ("cadwyn.structure.versions", "Version"),
        "VersionBundle": ("cadwyn.structure.versions", "VersionBundle"),
        "VersionChange": ("cadwyn.structure.versions", "VersionChange"),
        "VersionChangeWithSideEffects": ("cadwyn.structure.versions", "VersionChangeWithSideEffects"),
        "convert_request_to_next_version_for": ("cadwyn.structure.data", "convert_request_to_next_version_for"),
        "convert_response_to_previous_version_for": (
            "cadwyn.structure.data",
            "convert_response_to_previous_version_for",
        ),
        "endpoint": ("cadwyn.structure.endpoints", "endpoint"),
        "enum": ("cadwyn.structure.enums", "enum"),
        "schema": ("cadwyn.structure.schemas", "schema"),
    },
    "cadwyn.schema_generation": {
        "PydanticFieldWrapper": ("cadwyn.schema_generation", "PydanticFieldWrapper"),
        "SchemaGenerator": ("cadwyn.schema_generation", "SchemaGenerator"),
        "_AnnotationTransformer": ("cadwyn.schema_generation", "_AnnotationTransformer"),
        "_AsyncCallableWrapper": ("cadwyn.schema_generation", "_AsyncCallableWrapper"),
        "_AsyncGeneratorCallableWrapper": ("cadwyn.schema_generation", "_AsyncGeneratorCallableWrapper"),
        "_Call": ("cadwyn.schema_generation", None),
        "_CallableKind": ("cadwyn.schema_generation", "_CallableKind"),
        "_CallableWrapper": ("cadwyn.schema_generation", "_CallableWrapper"),
        "_CycleCopier": ("cadwyn.schema_generation", "_CycleCopier"),
        "_DeletedField": ("cadwyn.schema_generation", "_DeletedField"),
        "_DummyEnum": ("cadwyn.schema_generation", "_DummyEnum"),
        "_DummyFlag": ("cadwyn.schema_generation", "_DummyFlag"),
        "_EnumWrapper": ("cadwyn.schema_generation", "_EnumWrapper"),
        "_FieldAttributeChange": ("cadwyn.schema_generation", "_FieldAttributeChange"),
        "_FieldRename": ("cadwyn.schema_generation", "_FieldRename"),
        "_GenerationAnalysis": ("cadwyn.schema_generation", "_GenerationAnalysis"),
        "_GeneratorCallableWrapper": ("cadwyn.schema_generation", "_GeneratorCallableWrapper"),
        "_ModelBundle": ("cadwyn.schema_generation", "_ModelBundle"),
        "_ModelReferences": ("cadwyn.schema_generation", "_ModelReferences"),
        "_ParentRename": ("cadwyn.schema_generation", "_ParentRename"),
        "_PerFieldValidatorWrapper": ("cadwyn.schema_generation", "_PerFieldValidatorWrapper"),
        "_PydanticModelWrapper": ("cadwyn.schema_generation", "_PydanticModelWrapper"),
        "_ReferenceCycles": ("cadwyn.schema_generation", "_ReferenceCycles"),
        "_ReferencedModelCollector": ("cadwyn.schema_generation", "_ReferencedModelCollector"),
        "_RuntimeSchemaGenContext": ("cadwyn.schema_generation", "_RuntimeSchemaGenContext"),
        "_SchemaChanges": ("cadwyn.schema_generation", "_SchemaChanges"),
        "_T_ANY_MODEL": ("cadwyn.schema_generation", None),
        "_T_COMPONENT_VALUE": ("cadwyn.schema_generation", None),
        "_T_ENUM": ("cadwyn.schema_generation", None),
        "_T_EVALUATED": ("cadwyn.schema_generation", None),
        "_T_NODE": ("cadwyn.schema_generation", None),
        "_T_PYDANTIC_MODEL": ("cadwyn.schema_generation", None),
        "_TypeAliasRefersToItselfError": ("cadwyn.schema_generation", "_TypeAliasRefersToItselfError"),
        "_TypeAliasValue": ("cadwyn.schema_generation", "_TypeAliasValue"),
        "_ValidatorWrapper": ("cadwyn.schema_generation", "_ValidatorWrapper"),
        "_ValueOfMemberWithOnlyItsValue": ("cadwyn.schema_generation", "_ValueOfMemberWithOnlyItsValue"),
        "_VersioningQuestion": ("cadwyn.schema_generation", "_VersioningQuestion"),
        "_add_field_to_model": ("cadwyn.schema_generation", "_add_field_to_model"),
        "_add_request_and_response_params": ("cadwyn.schema_generation", "_add_request_and_response_params"),
        "_alias_value_uses_fallbacks": ("cadwyn.schema_generation", "_alias_value_uses_fallbacks"),
        "_apply_alter_enum_instructions": ("cadwyn.schema_generation", "_apply_alter_enum_instructions"),
        "_apply_alter_schema_instructions": ("cadwyn.schema_generation", "_apply_alter_schema_instructions"),
        "_are_same_args": ("cadwyn.schema_generation", "_are_same_args"),
        "_change_annotated_metadata": ("cadwyn.schema_generation", "_change_annotated_metadata"),
        "_change_field": ("cadwyn.schema_generation", "_change_field"),
        "_change_field_in_model": ("cadwyn.schema_generation", "_change_field_in_model"),
        "_change_model": ("cadwyn.schema_generation", "_change_model"),
        "_change_model_config": ("cadwyn.schema_generation", "_change_model_config"),
        "_change_version_of_generic_metadata": ("cadwyn.schema_generation", "_change_version_of_generic_metadata"),
        "_change_version_of_model_bases": ("cadwyn.schema_generation", "_change_version_of_model_bases"),
        "_change_version_of_model_references": ("cadwyn.schema_generation", "_change_version_of_model_references"),
        "_check_that_deleted_fields_are_not_inherited": (
            "cadwyn.schema_generation",
            "_check_that_deleted_fields_are_not_inherited",
        ),
        "_check_that_renamed_fields_are_not_inherited": (
            "cadwyn.schema_generation",
            "_check_that_renamed_fields_are_not_inherited",
        ),
        "_contains_forward_refs": ("cadwyn.schema_generation", "_contains_forward_refs"),
        "_contains_only_builtin_types": ("cadwyn.schema_generation", "_contains_only_builtin_types"),
        "_contains_pydantic_recursive_refs": ("cadwyn.schema_generation", "_contains_pydantic_recursive_refs"),
        "_copy_field_info": ("cadwyn.schema_generation", "_copy_field_info"),
        "_copy_of_parent_has_field": ("cadwyn.schema_generation", "_copy_of_parent_has_field"),
        "_create_model_bundle": ("cadwyn.schema_generation", "_create_model_bundle"),
        "_declares_name": ("cadwyn.schema_generation", "_declares_name"),
        "_delete_field_attributes": ("cadwyn.schema_generation", "_delete_field_attributes"),
        "_delete_field_from_model": ("cadwyn.schema_generation", "_delete_field_from_model"),
        "_delete_metadata_attribute": ("cadwyn.schema_generation", "_delete_metadata_attribute"),
        "_eval_forward_refs": ("cadwyn.schema_generation", "_eval_forward_refs"),
        "_eval_generic_args": ("cadwyn.schema_generation", "_eval_generic_args"),
        "_evaluate_user_annotations": ("cadwyn.schema_generation", "_evaluate_user_annotations"),
        "_extract_passed_field_attributes": ("cadwyn.schema_generation", "_extract_passed_field_attributes"),
        "_find_strongly_connected_components": ("cadwyn.schema_generation", "_find_strongly_connected_components"),
        "_format_bases": ("cadwyn.schema_generation", "_format_bases"),
        "_function_globals": ("cadwyn.schema_generation", "_function_globals"),
        "_get_aliases_derived_from_alias": ("cadwyn.schema_generation", "_get_aliases_derived_from_alias"),
        "_get_annotation_with_metadata": ("cadwyn.schema_generation", "_get_annotation_with_metadata"),
        "_get_annotations_without_evaluating": ("cadwyn.schema_generation", "_get_annotations_without_evaluating"),
        "_get_bases_that_give_inherited_field": ("cadwyn.schema_generation", "_get_bases_that_give_inherited_field"),
        "_get_best_effort_globals": ("cadwyn.schema_generation", "_get_best_effort_globals"),
        "_get_classes_in_core_schema": ("cadwyn.schema_generation", "_get_classes_in_core_schema"),
        "_get_core_schema_definitions": ("cadwyn.schema_generation", "_get_core_schema_definitions"),
        "_get_cycle_name": ("cadwyn.schema_generation", "_get_cycle_name"),
        "_get_deep_copy_error": ("cadwyn.schema_generation", "_get_deep_copy_error"),
        "_get_description": ("cadwyn.schema_generation", "_get_description"),
        "_get_extra_fields_annotation": ("cadwyn.schema_generation", "_get_extra_fields_annotation"),
        "_get_field_core_schemas": ("cadwyn.schema_generation", "_get_field_core_schemas"),
        "_get_field_identities": ("cadwyn.schema_generation", "_get_field_identities"),
        "_get_first_renames": ("cadwyn.schema_generation", "_get_first_renames"),
        "_get_function_behind_callable": ("cadwyn.schema_generation", "_get_function_behind_callable"),
        "_get_globals": ("cadwyn.schema_generation", "_get_globals"),
        "_get_head_model_references": ("cadwyn.schema_generation", "_get_head_model_references"),
        "_get_instructions_by_model": ("cadwyn.schema_generation", "_get_instructions_by_model"),
        "_get_latest_generic_args_error": ("cadwyn.schema_generation", "_get_latest_generic_args_error"),
        "_get_member_state": ("cadwyn.schema_generation", "_get_member_state"),
        "_get_model_annotations": ("cadwyn.schema_generation", "_get_model_annotations"),
        "_get_model_field_names": ("cadwyn.schema_generation", "_get_model_field_names"),
        "_get_models_in_field": ("cadwyn.schema_generation", "_get_models_in_field"),
        "_get_models_validated_by_fields": ("cadwyn.schema_generation", "_get_models_validated_by_fields"),
        "_get_models_with_instructions": ("cadwyn.schema_generation", "_get_models_with_instructions"),
        "_get_module_globals": ("cadwyn.schema_generation", "_get_module_globals"),
        "_get_name_of_field": ("cadwyn.schema_generation", "_get_name_of_field"),
        "_get_own_slot_names": ("cadwyn.schema_generation", "_get_own_slot_names"),
        "_get_parent_rename_error": ("cadwyn.schema_generation", "_get_parent_rename_error"),
        "_get_parent_renames": ("cadwyn.schema_generation", "_get_parent_renames"),
        "_get_parent_that_renamed_field": ("cadwyn.schema_generation", "_get_parent_that_renamed_field"),
        "_get_rebuilt_model_fields": ("cadwyn.schema_generation", "_get_rebuilt_model_fields"),
        "_get_signature": ("cadwyn.schema_generation", "_get_signature"),
        "_get_sort_key_of_model": ("cadwyn.schema_generation", "_get_sort_key_of_model"),
        "_get_subclasses_that_can_be_versioned": ("cadwyn.schema_generation", "_get_subclasses_that_can_be_versioned"),
        "_get_subclasses_that_share_field": ("cadwyn.schema_generation", "_get_subclasses_that_share_field"),
        "_get_typevar_uses": ("cadwyn.schema_generation", "_get_typevar_uses"),
        "_get_unevaluated_signature": ("cadwyn.schema_generation", "_get_unevaluated_signature"),
        "_get_unset_metadata_attribute_value": ("cadwyn.schema_generation", "_get_unset_metadata_attribute_value"),
        "_get_value_of_member_with_only_its_value": (
            "cadwyn.schema_generation",
            "_get_value_of_member_with_only_its_value",
        ),
        "_get_value_of_type_alias_with_args": ("cadwyn.schema_generation", "_get_value_of_type_alias_with_args"),
        "_has_custom_new": ("cadwyn.schema_generation", "_has_custom_new"),
        "_has_default_and_default_factory": ("cadwyn.schema_generation", "_has_default_and_default_factory"),
        "_has_only_its_value": ("cadwyn.schema_generation", "_has_only_its_value"),
        "_has_undefined_names_in_lazy_annotations": (
            "cadwyn.schema_generation",
            "_has_undefined_names_in_lazy_annotations",
        ),
        "_is_dunder": ("cadwyn.schema_generation", "_is_dunder"),
        "_is_generic_model": ("cadwyn.schema_generation", "_is_generic_model"),
        "_is_one_of": ("cadwyn.schema_generation", "_is_one_of"),
        "_is_parametrization": ("cadwyn.schema_generation", "_is_parametrization"),
        "_is_pydantic_model_class": ("cadwyn.schema_generation", "_is_pydantic_model_class"),
        "_is_same_member": ("cadwyn.schema_generation", "_is_same_member"),
        "_is_same_or_interchangeable": ("cadwyn.schema_generation", "_is_same_or_interchangeable"),
        "_is_same_parametrization": ("cadwyn.schema_generation", "_is_same_parametrization"),
        "_is_same_timezone": ("cadwyn.schema_generation", "_is_same_timezone"),
        "_is_type_position_in_generic": ("cadwyn.schema_generation", "_is_type_position_in_generic"),
        "_is_unresolved_annotation": ("cadwyn.schema_generation", "_is_unresolved_annotation"),
        "_is_versionable_model": ("cadwyn.schema_generation", "_is_versionable_model"),
        "_iter_core_schema_nodes": ("cadwyn.schema_generation", "_iter_core_schema_nodes"),
        "_iter_values_within": ("cadwyn.schema_generation", "_iter_values_within"),
        "_keep_last_of_default_and_default_factory": (
            "cadwyn.schema_generation",
            "_keep_last_of_default_and_default_factory",
        ),
        "_make_derived_aliases_follow_alias": ("cadwyn.schema_generation", "_make_derived_aliases_follow_alias"),
        "_make_init_that_skips_members_with_only_their_values": (
            "cadwyn.schema_generation",
            "_make_init_that_skips_members_with_only_their_values",
        ),
        "_make_new_that_keeps_members_with_only_their_values": (
            "cadwyn.schema_generation",
            "_make_new_that_keeps_members_with_only_their_values",
        ),
        "_make_subclasses_follow_rename": ("cadwyn.schema_generation", "_make_subclasses_follow_rename"),
        "_metadata_attribute_can_be_deleted": ("cadwyn.schema_generation", "_metadata_attribute_can_be_deleted"),
        "_metadata_has_attribute": ("cadwyn.schema_generation", "_metadata_has_attribute"),
        "_migrate_classes": ("cadwyn.schema_generation", "_migrate_classes"),
        "_only_changes_fields": ("cadwyn.schema_generation", "_only_changes_fields"),
        "_rebuild_generic_alias": ("cadwyn.schema_generation", "_rebuild_generic_alias"),
        "_rebuild_model": ("cadwyn.schema_generation", "_rebuild_model"),
        "_replace_forward_refs": ("cadwyn.schema_generation", "_replace_forward_refs"),
        "_replace_in_type_positions": ("cadwyn.schema_generation", "_replace_in_type_positions"),
        "_restore_member_state": ("cadwyn.schema_generation", "_restore_member_state"),
        "_set_keys_renamed_by_alias": ("cadwyn.schema_generation", "_set_keys_renamed_by_alias"),
        "_sets_default_attribute": ("cadwyn.schema_generation", "_sets_default_attribute"),
        "_shares_attribute_objects": ("cadwyn.schema_generation", "_shares_attribute_objects"),
        "_substitute_type_vars_of_inherited_annotation": (
            "cadwyn.schema_generation",
            "_substitute_type_vars_of_inherited_annotation",
        ),
        "_take_head_members_by_value": ("cadwyn.schema_generation", "_take_head_members_by_value"),
        "_try_eval_type": ("cadwyn.schema_generation", "_try_eval_type"),
        "_try_eval_type_for_discovery": ("cadwyn.schema_generation", "_try_eval_type_for_discovery"),
        "_unwrap_model": ("cadwyn.schema_generation", "_unwrap_model"),
        "_unwrap_partial": ("cadwyn.schema_generation", "_unwrap_partial"),
        "_wrap_enum_nonmember": ("cadwyn.schema_generation", "_wrap_enum_nonmember"),
        "_wrap_pydantic_model": ("cadwyn.schema_generation", "_wrap_pydantic_model"),
        "_wrap_validator": ("cadwyn.schema_generation", "_wrap_validator"),
        "generate_versioned_models": ("cadwyn.schema_generation", "generate_versioned_models"),
        "get_models_that_routes_request": ("cadwyn.schema_generation", "get_models_that_routes_request"),
        "is_async_gen_callable": ("cadwyn.schema_generation", "is_async_gen_callable"),
        "is_coroutine_callable": ("cadwyn.schema_generation", "is_coroutine_callable"),
        "is_gen_callable": ("cadwyn.schema_generation", "is_gen_callable"),
        "is_regular_function": ("cadwyn.schema_generation", "is_regular_function"),
        "migrate_response_body": ("cadwyn.schema_generation", "migrate_response_body"),
    },
    "cadwyn.structure.versions": {
        "HeadVersion": ("cadwyn.structure.versions", "HeadVersion"),
        "Version": ("cadwyn.structure.versions", "Version"),
        "VersionBundle": ("cadwyn.structure.versions", "VersionBundle"),
        "VersionChange": ("cadwyn.structure.versions", "VersionChange"),
        "VersionChangeWithSideEffects": ("cadwyn.structure.versions", "VersionChangeWithSideEffects"),
        "_BodyParamKind": ("cadwyn.structure.versions", "_BodyParamKind"),
        "_BodyParamLookup": ("cadwyn.structure.versions", "_BodyParamLookup"),
        "_ClientJsonStrings": ("cadwyn.structure.versions", "_ClientJsonStrings"),
        "_ClientJsonStringsCheck": ("cadwyn.structure.versions", "_ClientJsonStringsCheck"),
        "_DroppedBackgroundTasksWarning": ("cadwyn.structure.versions", "_DroppedBackgroundTasksWarning"),
        "_ExpectedFormValue": ("cadwyn.structure.versions", "_ExpectedFormValue"),
        "_FormModelFields": ("cadwyn.structure.versions", "_FormModelFields"),
        "_FormModelLookup": ("cadwyn.structure.versions", "_FormModelLookup"),
        "_FormModelOmission": ("cadwyn.structure.versions", "_FormModelOmission"),
        "_FormModelReader": ("cadwyn.structure.versions", "_FormModelReader"),
        "_FormUnionValueError": ("cadwyn.structure.versions", "_FormUnionValueError"),
        "_FormValueMarker": ("cadwyn.structure.versions", "_FormValueMarker"),
        "_ModelInputPlan": ("cadwyn.structure.versions", "_ModelInputPlan"),
        "_P": ("cadwyn.structure.versions", None),
        "_R": ("cadwyn.structure.versions", None),
        "_RestorePlan": ("cadwyn.structure.versions", "_RestorePlan"),
        "_SchemaInstruction": ("cadwyn.structure.versions", None),
        "_SchemaMigrationTarget": ("cadwyn.structure.versions", "_SchemaMigrationTarget"),
        "_UnsafeJsonValueError": ("cadwyn.structure.versions", "_UnsafeJsonValueError"),
        "_UploadReadByFastapi": ("cadwyn.structure.versions", "_UploadReadByFastapi"),
        "_UploadsToClose": ("cadwyn.structure.versions", "_UploadsToClose"),
        "_VirtualForm": ("cadwyn.structure.versions", "_VirtualForm"),
        "_add_keyword_only_parameter": ("cadwyn.structure.versions", "_add_keyword_only_parameter"),
        "_annotation_reaches_models_with": ("cadwyn.structure.versions", "_annotation_reaches_models_with"),
        "_are_json_keys": ("cadwyn.structure.versions", "_are_json_keys"),
        "_as_form_value": ("cadwyn.structure.versions", "_as_form_value"),
        "_body_is_in_kwargs": ("cadwyn.structure.versions", "_body_is_in_kwargs"),
        "_build_form_model_fields": ("cadwyn.structure.versions", "_build_form_model_fields"),
        "_can_be_called_with": ("cadwyn.structure.versions", "_can_be_called_with"),
        "_cant_pass_form_error": ("cadwyn.structure.versions", "_cant_pass_form_error"),
        "_copy_container_with_children": ("cadwyn.structure.versions", "_copy_container_with_children"),
        "_copy_decoded_json_string": ("cadwyn.structure.versions", "_copy_decoded_json_string"),
        "_copy_decoded_json_strings": ("cadwyn.structure.versions", "_copy_decoded_json_strings"),
        "_describe_body_param": ("cadwyn.structure.versions", "_describe_body_param"),
        "_describe_form_union_value_error": ("cadwyn.structure.versions", "_describe_form_union_value_error"),
        "_describe_json_model": ("cadwyn.structure.versions", "_describe_json_model"),
        "_describe_unknown_value": ("cadwyn.structure.versions", "_describe_unknown_value"),
        "_do_body_readers_share_plan": ("cadwyn.structure.versions", "_do_body_readers_share_plan"),
        "_do_compiled_aliases_match": ("cadwyn.structure.versions", "_do_compiled_aliases_match"),
        "_do_compiled_fields_match": ("cadwyn.structure.versions", "_do_compiled_fields_match"),
        "_do_compiled_settings_match": ("cadwyn.structure.versions", "_do_compiled_settings_match"),
        "_does_rename_apply": ("cadwyn.structure.versions", "_does_rename_apply"),
        "_dump_request_body": ("cadwyn.structure.versions", "_dump_request_body"),
        "_dumps_like": ("cadwyn.structure.versions", "_dumps_like"),
        "_embed_form_model": ("cadwyn.structure.versions", "_embed_form_model"),
        "_encode_json_value": ("cadwyn.structure.versions", "_encode_json_value"),
        "_encode_json_values": ("cadwyn.structure.versions", "_encode_json_values"),
        "_extract_form_value": ("cadwyn.structure.versions", "_extract_form_value"),
        "_fastapi_got_no_body": ("cadwyn.structure.versions", "_fastapi_got_no_body"),
        "_finish_restoring": ("cadwyn.structure.versions", "_finish_restoring"),
        "_form_model_gets_value": ("cadwyn.structure.versions", "_form_model_gets_value"),
        "_form_model_union_changes_body": ("cadwyn.structure.versions", "_form_model_union_changes_body"),
        "_form_supplies_form_model": ("cadwyn.structure.versions", "_form_supplies_form_model"),
        "_get_body": ("cadwyn.structure.versions", "_get_body"),
        "_get_body_field_json_plan": ("cadwyn.structure.versions", "_get_body_field_json_plan"),
        "_get_body_input_plan": ("cadwyn.structure.versions", "_get_body_input_plan"),
        "_get_body_lookups": ("cadwyn.structure.versions", "_get_body_lookups"),
        "_get_bytes_file_params_of_dependencies": (
            "cadwyn.structure.versions",
            "_get_bytes_file_params_of_dependencies",
        ),
        "_get_bytes_file_params_of_fields": ("cadwyn.structure.versions", "_get_bytes_file_params_of_fields"),
        "_get_child_core_schemas": ("cadwyn.structure.versions", "_get_child_core_schemas"),
        "_get_children_that_can_be_enum_members": (
            "cadwyn.structure.versions",
            "_get_children_that_can_be_enum_members",
        ),
        "_get_closest_union_member": ("cadwyn.structure.versions", "_get_closest_union_member"),
        "_get_dependency_overrides": ("cadwyn.structure.versions", "_get_dependency_overrides"),
        "_get_dumped_key": ("cadwyn.structure.versions", "_get_dumped_key"),
        "_get_expected_form_model_value": ("cadwyn.structure.versions", "_get_expected_form_model_value"),
        "_get_expected_values_of_body_params": ("cadwyn.structure.versions", "_get_expected_values_of_body_params"),
        "_get_failing_keys": ("cadwyn.structure.versions", "_get_failing_keys"),
        "_get_failing_renames": ("cadwyn.structure.versions", "_get_failing_renames"),
        "_get_field_aliases_of_models": ("cadwyn.structure.versions", "_get_field_aliases_of_models"),
        "_get_field_serializers": ("cadwyn.structure.versions", "_get_field_serializers"),
        "_get_fields_core_schema": ("cadwyn.structure.versions", "_get_fields_core_schema"),
        "_get_form_for_omitted_form_model": ("cadwyn.structure.versions", "_get_form_for_omitted_form_model"),
        "_get_form_model_aliases": ("cadwyn.structure.versions", "_get_form_model_aliases"),
        "_get_form_model_fields": ("cadwyn.structure.versions", "_get_form_model_fields"),
        "_get_form_model_lookup": ("cadwyn.structure.versions", "_get_form_model_lookup"),
        "_get_form_model_omission": ("cadwyn.structure.versions", "_get_form_model_omission"),
        "_get_form_models_before_fastapi_0_161": ("cadwyn.structure.versions", "_get_form_models_before_fastapi_0_161"),
        "_get_form_union_body": ("cadwyn.structure.versions", "_get_form_union_body"),
        "_get_form_union_readers": ("cadwyn.structure.versions", "_get_form_union_readers"),
        "_get_form_values": ("cadwyn.structure.versions", "_get_form_values"),
        "_get_hidden_background_tasks_param_name": (
            "cadwyn.structure.versions",
            "_get_hidden_background_tasks_param_name",
        ),
        "_get_input_plan": ("cadwyn.structure.versions", "_get_input_plan"),
        "_get_input_union_members": ("cadwyn.structure.versions", "_get_input_union_members"),
        "_get_json_path_key": ("cadwyn.structure.versions", "_get_json_path_key"),
        "_get_json_values_plan": ("cadwyn.structure.versions", "_get_json_values_plan"),
        "_get_keys_read_by_fields": ("cadwyn.structure.versions", "_get_keys_read_by_fields"),
        "_get_keys_to_switch": ("cadwyn.structure.versions", "_get_keys_to_switch"),
        "_get_known_form_param_names": ("cadwyn.structure.versions", "_get_known_form_param_names"),
        "_get_lookup_aliases": ("cadwyn.structure.versions", "_get_lookup_aliases"),
        "_get_model_height": ("cadwyn.structure.versions", "_get_model_height"),
        "_get_model_inner_core_schema": ("cadwyn.structure.versions", "_get_model_inner_core_schema"),
        "_get_model_input_plan": ("cadwyn.structure.versions", "_get_model_input_plan"),
        "_get_models_of_schema": ("cadwyn.structure.versions", "_get_models_of_schema"),
        "_get_models_validated_as_form_data": ("cadwyn.structure.versions", "_get_models_validated_as_form_data"),
        "_get_normalized_core_schema": ("cadwyn.structure.versions", "_get_normalized_core_schema"),
        "_get_omission_of_optional_form_model": ("cadwyn.structure.versions", "_get_omission_of_optional_form_model"),
        "_get_omitted_body_marker": ("cadwyn.structure.versions", "_get_omitted_body_marker"),
        "_get_omitted_form_model_lookup": ("cadwyn.structure.versions", "_get_omitted_form_model_lookup"),
        "_get_other_validation_keys": ("cadwyn.structure.versions", "_get_other_validation_keys"),
        "_get_param_with_other_value": ("cadwyn.structure.versions", "_get_param_with_other_value"),
        "_get_rename_candidates": ("cadwyn.structure.versions", "_get_rename_candidates"),
        "_get_renamed_key_checks": ("cadwyn.structure.versions", "_get_renamed_key_checks"),
        "_get_renames": ("cadwyn.structure.versions", "_get_renames"),
        "_get_renames_and_traversable_fields": ("cadwyn.structure.versions", "_get_renames_and_traversable_fields"),
        "_get_request_body_target": ("cadwyn.structure.versions", "_get_request_body_target"),
        "_get_restorable_models": ("cadwyn.structure.versions", "_get_restorable_models"),
        "_get_restorable_node": ("cadwyn.structure.versions", "_get_restorable_node"),
        "_get_restore_plan": ("cadwyn.structure.versions", "_get_restore_plan"),
        "_get_restorer_spec": ("cadwyn.structure.versions", "_get_restorer_spec"),
        "_get_route_json_values_plan": ("cadwyn.structure.versions", "_get_route_json_values_plan"),
        "_get_schema_migration_key": ("cadwyn.structure.versions", "_get_schema_migration_key"),
        "_get_schema_migration_target": ("cadwyn.structure.versions", "_get_schema_migration_target"),
        "_get_schema_migrations": ("cadwyn.structure.versions", "_get_schema_migrations"),
        "_get_schema_shape": ("cadwyn.structure.versions", "_get_schema_shape"),
        "_get_spec_height": ("cadwyn.structure.versions", "_get_spec_height"),
        "_get_strict_content_type": ("cadwyn.structure.versions", "_get_strict_content_type"),
        "_get_unchanging_model_core_schema": ("cadwyn.structure.versions", "_get_unchanging_model_core_schema"),
        "_get_union_input_plan": ("cadwyn.structure.versions", "_get_union_input_plan"),
        "_get_union_member_of_body": ("cadwyn.structure.versions", "_get_union_member_of_body"),
        "_get_union_member_of_response": ("cadwyn.structure.versions", "_get_union_member_of_response"),
        "_get_union_members": ("cadwyn.structure.versions", "_get_union_members"),
        "_get_unsupported_fastapi_functions": ("cadwyn.structure.versions", "_get_unsupported_fastapi_functions"),
        "_get_uploads_read_by_fastapi": ("cadwyn.structure.versions", "_get_uploads_read_by_fastapi"),
        "_get_uploads_to_close": ("cadwyn.structure.versions", "_get_uploads_to_close"),
        "_get_validation_paths": ("cadwyn.structure.versions", "_get_validation_paths"),
        "_get_validation_settings": ("cadwyn.structure.versions", "_get_validation_settings"),
        "_get_virtual_form": ("cadwyn.structure.versions", "_get_virtual_form"),
        "_has_core_schema_of_types": ("cadwyn.structure.versions", "_has_core_schema_of_types"),
        "_has_fields_of_head_model": ("cadwyn.structure.versions", "_has_fields_of_head_model"),
        "_has_json_values_to_encode": ("cadwyn.structure.versions", "_has_json_values_to_encode"),
        "_has_other_body_readers": ("cadwyn.structure.versions", "_has_other_body_readers"),
        "_has_union_member_migrations": ("cadwyn.structure.versions", "_has_union_member_migrations"),
        "_has_unsafe_core_schema": ("cadwyn.structure.versions", "_has_unsafe_core_schema"),
        "_is_body_field_safe": ("cadwyn.structure.versions", "_is_body_field_safe"),
        "_is_client_json_string": ("cadwyn.structure.versions", "_is_client_json_string"),
        "_is_core_schema_unit_safe": ("cadwyn.structure.versions", "_is_core_schema_unit_safe"),
        "_is_custom_serialization": ("cadwyn.structure.versions", "_is_custom_serialization"),
        "_is_dumped_from_models": ("cadwyn.structure.versions", "_is_dumped_from_models"),
        "_is_field_dumped_like": ("cadwyn.structure.versions", "_is_field_dumped_like"),
        "_is_field_schema_safe": ("cadwyn.structure.versions", "_is_field_schema_safe"),
        "_is_json_container": ("cadwyn.structure.versions", "_is_json_container"),
        "_is_json_temporal": ("cadwyn.structure.versions", "_is_json_temporal"),
        "_is_json_value": ("cadwyn.structure.versions", "_is_json_value"),
        "_is_json_value_to_encode": ("cadwyn.structure.versions", "_is_json_value_to_encode"),
        "_is_nan": ("cadwyn.structure.versions", "_is_nan"),
        "_is_nested_key_validated": ("cadwyn.structure.versions", "_is_nested_key_validated"),
        "_is_plain_json_schema": ("cadwyn.structure.versions", "_is_plain_json_schema"),
        "_is_renamed_key_check": ("cadwyn.structure.versions", "_is_renamed_key_check"),
        "_is_same_dict": ("cadwyn.structure.versions", "_is_same_dict"),
        "_iter_children_to_restore": ("cadwyn.structure.versions", "_iter_children_to_restore"),
        "_iter_dependants_that_fastapi_solves": ("cadwyn.structure.versions", "_iter_dependants_that_fastapi_solves"),
        "_iter_dependencies": ("cadwyn.structure.versions", "_iter_dependencies"),
        "_iter_nested_models": ("cadwyn.structure.versions", "_iter_nested_models"),
        "_iter_restorable_models": ("cadwyn.structure.versions", "_iter_restorable_models"),
        "_iter_uploads_read_to_the_end": ("cadwyn.structure.versions", "_iter_uploads_read_to_the_end"),
        "_model_needs_restoring": ("cadwyn.structure.versions", "_model_needs_restoring"),
        "_model_to_input": ("cadwyn.structure.versions", "_model_to_input"),
        "_needs_restoring": ("cadwyn.structure.versions", "_needs_restoring"),
        "_no_union_member": ("cadwyn.structure.versions", "_no_union_member"),
        "_normalize_core_schema": ("cadwyn.structure.versions", "_normalize_core_schema"),
        "_normalize_validation_alias": ("cadwyn.structure.versions", "_normalize_validation_alias"),
        "_overrides_dumping": ("cadwyn.structure.versions", "_overrides_dumping"),
        "_passes_json_value_check": ("cadwyn.structure.versions", "_passes_json_value_check"),
        "_pick_form_value": ("cadwyn.structure.versions", "_pick_form_value"),
        "_plan_matches": ("cadwyn.structure.versions", "_plan_matches"),
        "_prepare_response_content": ("cadwyn.structure.versions", "_prepare_response_content"),
        "_prune_json_values_description": ("cadwyn.structure.versions", "_prune_json_values_description"),
        "_raise_unknown_union_member_of_body": ("cadwyn.structure.versions", "_raise_unknown_union_member_of_body"),
        "_reaches_models_with": ("cadwyn.structure.versions", "_reaches_models_with"),
        "_register_core_schema_definitions": ("cadwyn.structure.versions", "_register_core_schema_definitions"),
        "_reject_keys_renamed_by_alias": ("cadwyn.structure.versions", "_reject_keys_renamed_by_alias"),
        "_rename_keys": ("cadwyn.structure.versions", "_rename_keys"),
        "_replace_planned_json_values": ("cadwyn.structure.versions", "_replace_planned_json_values"),
        "_replace_with_head_enum_member": ("cadwyn.structure.versions", "_replace_with_head_enum_member"),
        "_replace_with_head_enum_members": ("cadwyn.structure.versions", "_replace_with_head_enum_members"),
        "_rescue_body": ("cadwyn.structure.versions", "_rescue_body"),
        "_rescue_model_data": ("cadwyn.structure.versions", "_rescue_model_data"),
        "_rescue_response": ("cadwyn.structure.versions", "_rescue_response"),
        "_resolve_type_aliases": ("cadwyn.structure.versions", "_resolve_type_aliases"),
        "_restore_data": ("cadwyn.structure.versions", "_restore_data"),
        "_restore_shallow_data": ("cadwyn.structure.versions", "_restore_shallow_data"),
        "_restore_shallow_model_data": ("cadwyn.structure.versions", "_restore_shallow_model_data"),
        "_restore_surely_failing_data": ("cadwyn.structure.versions", "_restore_surely_failing_data"),
        "_rewind_uploads_read_by_fastapi": ("cadwyn.structure.versions", "_rewind_uploads_read_by_fastapi"),
        "_run_request_migration": ("cadwyn.structure.versions", "_run_request_migration"),
        "_set_restored_child": ("cadwyn.structure.versions", "_set_restored_child"),
        "_split_spec": ("cadwyn.structure.versions", "_split_spec"),
        "_summarize_core_schema_unit": ("cadwyn.structure.versions", "_summarize_core_schema_unit"),
        "_takes_single_form_value": ("cadwyn.structure.versions", "_takes_single_form_value"),
        "_to_head_input": ("cadwyn.structure.versions", "_to_head_input"),
        "_to_input_by_plan": ("cadwyn.structure.versions", "_to_input_by_plan"),
        "_unembed_form_model_errors": ("cadwyn.structure.versions", "_unembed_form_model_errors"),
        "_unwrap_default_factory": ("cadwyn.structure.versions", "_unwrap_default_factory"),
        "_unwrap_input_schema": ("cadwyn.structure.versions", "_unwrap_input_schema"),
        "_walk_json_leaf": ("cadwyn.structure.versions", "_walk_json_leaf"),
        "_walk_json_model": ("cadwyn.structure.versions", "_walk_json_model"),
        "_walk_json_reference": ("cadwyn.structure.versions", "_walk_json_reference"),
        "_walk_json_schema": ("cadwyn.structure.versions", "_walk_json_schema"),
        "_walk_json_tuple": ("cadwyn.structure.versions", "_walk_json_tuple"),
        "_walk_json_union": ("cadwyn.structure.versions", "_walk_json_union"),
        "_walk_json_values": ("cadwyn.structure.versions", "_walk_json_values"),
        "_with_form_model_under_own_key": ("cadwyn.structure.versions", "_with_form_model_under_own_key"),
        "_with_form_models_under_own_key": ("cadwyn.structure.versions", "_with_form_models_under_own_key"),
        "get_cls_pythonpath": ("cadwyn.structure.versions", "get_cls_pythonpath"),
    },
}


def _module_and_qualname(value: Any) -> tuple[str, Union[str, None]]:
    return value.__module__, getattr(value, "__qualname__", None)


def _is_definition(value: object) -> bool:
    return isinstance(value, type | FunctionType | TypeVar | ParamSpec) or isinstance(
        getattr(value, "__wrapped__", None), FunctionType
    )


def _cadwyn_definitions(module: ModuleType) -> Iterator[tuple[str, Any]]:
    for name, value in vars(module).items():
        if not name.startswith("__") and _is_definition(value) and (value.__module__ or "").startswith("cadwyn."):
            yield name, value


def _methods(cls: type) -> Iterator[Any]:
    for member in vars(cls).values():
        if isinstance(member, classmethod | staticmethod):
            member = member.__func__
        elif isinstance(member, property):
            member = member.fget
        elif isinstance(member, functools.cached_property | classproperty):
            member = member.func
        if isinstance(member, FunctionType):
            yield member


@pytest.mark.parametrize("module_name", list(_PRE_SPLIT_METADATA))
def test__module_and_qualname__of_names_importable_before_split__should_stay_the_same(module_name: str):
    module = importlib.import_module(module_name)
    expected = _PRE_SPLIT_METADATA[module_name]

    actual = {name: _module_and_qualname(getattr(module, name)) for name in expected if hasattr(module, name)}

    assert actual == expected


@pytest.mark.parametrize("module_name", list(_PRE_SPLIT_METADATA))
def test__module_and_qualname__of_every_importable_definition__should_point_to_the_public_module(module_name: str):
    module = importlib.import_module(module_name)

    for name, value in _cadwyn_definitions(module):
        assert not _PRIVATE_SUBMODULE_OF_SPLIT_MODULE.match(value.__module__), name
        path = getattr(value, "__qualname__", value.__name__)
        assert functools.reduce(getattr, path.split("."), importlib.import_module(value.__module__)) is value, name


@pytest.mark.parametrize(
    ("module_name", "moved_method"),
    [("cadwyn.schema_generation", "SchemaGenerator.__init__"), ("cadwyn.structure.versions", "VersionBundle.__init__")],
)
def test__module__of_methods_of_classes_in_split_modules__should_not_name_private_submodules(
    module_name: str, moved_method: str
):
    module = importlib.import_module(module_name)
    classes = [value for _, value in _cadwyn_definitions(module) if isinstance(value, type)]

    modules_of_methods = {
        method.__qualname__: method.__module__
        for cls in classes
        if cls.__module__ == module_name
        for method in _methods(cls)
    }

    assert modules_of_methods[moved_method] == module_name
    assert {
        qualname: method_module
        for qualname, method_module in modules_of_methods.items()
        if _PRIVATE_SUBMODULE_OF_SPLIT_MODULE.match(method_module)
    } == {}


@pytest.mark.parametrize(
    "cls",
    [
        pytest.param(value, id=f"{module_name}.{name}")
        for module_name in _SPLIT_MODULES
        for name, value in _cadwyn_definitions(importlib.import_module(module_name))
        if isinstance(value, type) and value.__module__ == module_name
    ],
)
def test__pickle__of_classes_in_split_modules__should_restore_the_same_class(cls: type):
    assert pickle.loads(pickle.dumps(cls)) is cls


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(member, id=f"{module_name}.{name}.{member.name}")
        for module_name in _SPLIT_MODULES
        for name, value in _cadwyn_definitions(importlib.import_module(module_name))
        if isinstance(value, enum.EnumMeta) and value.__module__ == module_name
        for member in value
    ],
)
def test__pickle__of_enum_members_in_split_modules__should_restore_the_same_member(member: enum.Enum):
    assert pickle.loads(pickle.dumps(member)) is member
