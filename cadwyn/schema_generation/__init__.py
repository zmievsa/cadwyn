from pydantic._internal._generics import get_model_typevars_map as get_model_typevars_map
from pydantic._internal._model_construction import unpack_lenient_weakvaluedict as unpack_lenient_weakvaluedict

from ._annotations import (
    _BUILTIN_GENERICS_OF_RECURSIVE_TYPE_ALIASES as _BUILTIN_GENERICS_OF_RECURSIVE_TYPE_ALIASES,
)
from ._annotations import (
    _BUILTIN_TYPES_OF_RECURSIVE_TYPE_ALIASES as _BUILTIN_TYPES_OF_RECURSIVE_TYPE_ALIASES,
)
from ._annotations import (
    _NO_REDEFINED_FIELDS as _NO_REDEFINED_FIELDS,
)
from ._annotations import (
    _T_EVALUATED as _T_EVALUATED,
)
from ._annotations import (
    _TYPE_POSITION_GENERIC_MODULES as _TYPE_POSITION_GENERIC_MODULES,
)
from ._annotations import (
    _UNRESOLVED as _UNRESOLVED,
)
from ._annotations import (
    _are_same_args as _are_same_args,
)
from ._annotations import (
    _contains_forward_refs as _contains_forward_refs,
)
from ._annotations import (
    _contains_only_builtin_types as _contains_only_builtin_types,
)
from ._annotations import (
    _copy_field_info as _copy_field_info,
)
from ._annotations import (
    _eval_forward_refs as _eval_forward_refs,
)
from ._annotations import (
    _evaluate_user_annotations as _evaluate_user_annotations,
)
from ._annotations import (
    _get_annotation_with_metadata as _get_annotation_with_metadata,
)
from ._annotations import (
    _get_annotations_without_evaluating as _get_annotations_without_evaluating,
)
from ._annotations import (
    _get_extra_fields_annotation as _get_extra_fields_annotation,
)
from ._annotations import (
    _get_model_annotations as _get_model_annotations,
)
from ._annotations import (
    _get_model_field_names as _get_model_field_names,
)
from ._annotations import (
    _get_module_globals as _get_module_globals,
)
from ._annotations import (
    _get_rebuilt_model_fields as _get_rebuilt_model_fields,
)
from ._annotations import (
    _get_value_of_type_alias_with_args as _get_value_of_type_alias_with_args,
)
from ._annotations import (
    _has_undefined_names_in_lazy_annotations as _has_undefined_names_in_lazy_annotations,
)
from ._annotations import (
    _is_one_of as _is_one_of,
)
from ._annotations import (
    _is_type_position_in_generic as _is_type_position_in_generic,
)
from ._annotations import (
    _is_unresolved_annotation as _is_unresolved_annotation,
)
from ._annotations import (
    _rebuild_generic_alias as _rebuild_generic_alias,
)
from ._annotations import (
    _rebuild_model as _rebuild_model,
)
from ._annotations import (
    _replace_forward_refs as _replace_forward_refs,
)
from ._annotations import (
    _replace_in_type_positions as _replace_in_type_positions,
)
from ._annotations import (
    _try_eval_type as _try_eval_type,
)
from ._annotations import (
    _try_eval_type_for_discovery as _try_eval_type_for_discovery,
)
from ._annotations import (
    _TypeAliasRefersToItselfError as _TypeAliasRefersToItselfError,
)
from ._annotations import (
    _TypeAliasValue as _TypeAliasValue,
)
from ._callables import (
    _PARTIAL_DICT_ACCESSOR as _PARTIAL_DICT_ACCESSOR,
)
from ._callables import (
    _add_request_and_response_params as _add_request_and_response_params,
)
from ._callables import (
    _AsyncCallableWrapper as _AsyncCallableWrapper,
)
from ._callables import (
    _AsyncGeneratorCallableWrapper as _AsyncGeneratorCallableWrapper,
)
from ._callables import (
    _CallableKind as _CallableKind,
)
from ._callables import (
    _CallableWrapper as _CallableWrapper,
)
from ._callables import (
    _function_globals as _function_globals,
)
from ._callables import (
    _GeneratorCallableWrapper as _GeneratorCallableWrapper,
)
from ._callables import (
    _get_best_effort_globals as _get_best_effort_globals,
)
from ._callables import (
    _get_function_behind_callable as _get_function_behind_callable,
)
from ._callables import (
    _get_globals as _get_globals,
)
from ._callables import (
    _get_signature as _get_signature,
)
from ._callables import (
    _get_unevaluated_signature as _get_unevaluated_signature,
)
from ._callables import (
    _unwrap_partial as _unwrap_partial,
)
from ._callables import (
    is_async_gen_callable as is_async_gen_callable,
)
from ._callables import (
    is_coroutine_callable as is_coroutine_callable,
)
from ._callables import (
    is_gen_callable as is_gen_callable,
)
from ._callables import (
    is_regular_function as is_regular_function,
)
from ._common import (
    _DEFAULT_PYDANTIC_CLASSES as _DEFAULT_PYDANTIC_CLASSES,
)
from ._common import (
    _PYDANTIC_ALL_EXPORTED_NAMES as _PYDANTIC_ALL_EXPORTED_NAMES,
)
from ._common import (
    _T_ANY_MODEL as _T_ANY_MODEL,
)
from ._common import (
    _get_deep_copy_error as _get_deep_copy_error,
)
from ._common import (
    _is_dunder as _is_dunder,
)
from ._common import (
    _is_versionable_model as _is_versionable_model,
)
from ._common import (
    _unwrap_model as _unwrap_model,
)
from ._core_schema import (
    _CORE_SCHEMA_KEYS_WITHOUT_SCHEMAS as _CORE_SCHEMA_KEYS_WITHOUT_SCHEMAS,
)
from ._core_schema import (
    _get_classes_in_core_schema as _get_classes_in_core_schema,
)
from ._core_schema import (
    _get_core_schema_definitions as _get_core_schema_definitions,
)
from ._core_schema import (
    _get_field_core_schemas as _get_field_core_schemas,
)
from ._core_schema import (
    _iter_core_schema_nodes as _iter_core_schema_nodes,
)
from ._enums import (
    _CUSTOM_NEW_ADVICE as _CUSTOM_NEW_ADVICE,
)
from ._enums import (
    _ENUM_INTERNAL_ATTRIBUTES as _ENUM_INTERNAL_ATTRIBUTES,
)
from ._enums import (
    _ENUM_MEMBER_INTERNAL_ATTRIBUTES as _ENUM_MEMBER_INTERNAL_ATTRIBUTES,
)
from ._enums import (
    _FLAG_INTERNAL_ATTRIBUTES as _FLAG_INTERNAL_ATTRIBUTES,
)
from ._enums import (
    _INTERCHANGEABLE_VALUE_TYPES as _INTERCHANGEABLE_VALUE_TYPES,
)
from ._enums import (
    _STDLIB_ENUM_NEWS as _STDLIB_ENUM_NEWS,
)
from ._enums import (
    _T_ENUM as _T_ENUM,
)
from ._enums import (
    _UNSET_SLOT as _UNSET_SLOT,
)
from ._enums import (
    _USER_DEFINABLE_ENUM_ATTRIBUTES as _USER_DEFINABLE_ENUM_ATTRIBUTES,
)
from ._enums import (
    _apply_alter_enum_instructions as _apply_alter_enum_instructions,
)
from ._enums import (
    _DummyEnum as _DummyEnum,
)
from ._enums import (
    _DummyFlag as _DummyFlag,
)
from ._enums import (
    _EnumWrapper as _EnumWrapper,
)
from ._enums import (
    _get_member_state as _get_member_state,
)
from ._enums import (
    _get_own_slot_names as _get_own_slot_names,
)
from ._enums import (
    _get_value_of_member_with_only_its_value as _get_value_of_member_with_only_its_value,
)
from ._enums import (
    _has_custom_new as _has_custom_new,
)
from ._enums import (
    _has_only_its_value as _has_only_its_value,
)
from ._enums import (
    _is_enum_dunder as _is_enum_dunder,
)
from ._enums import (
    _is_enum_private as _is_enum_private,
)
from ._enums import (
    _is_enum_sunder as _is_enum_sunder,
)
from ._enums import (
    _is_same_member as _is_same_member,
)
from ._enums import (
    _is_same_or_interchangeable as _is_same_or_interchangeable,
)
from ._enums import (
    _is_same_timezone as _is_same_timezone,
)
from ._enums import (
    _make_init_that_skips_members_with_only_their_values as _make_init_that_skips_members_with_only_their_values,
)
from ._enums import (
    _make_new_that_keeps_members_with_only_their_values as _make_new_that_keeps_members_with_only_their_values,
)
from ._enums import (
    _restore_member_state as _restore_member_state,
)
from ._enums import (
    _take_head_members_by_value as _take_head_members_by_value,
)
from ._enums import (
    _ValueOfMemberWithOnlyItsValue as _ValueOfMemberWithOnlyItsValue,
)
from ._enums import (
    _wrap_enum_nonmember as _wrap_enum_nonmember,
)
from ._fields import (
    _FIELD_INFO_ATTRIBUTES_REMOVED_BY_DIDNT_HAVE as _FIELD_INFO_ATTRIBUTES_REMOVED_BY_DIDNT_HAVE,
)
from ._fields import (
    _OTHER_DEFAULT_ATTRIBUTE as _OTHER_DEFAULT_ATTRIBUTE,
)
from ._fields import (
    EXTRA_FIELD_NAME as EXTRA_FIELD_NAME,
)
from ._fields import (
    PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP as PYDANTIC_DECORATOR_TYPE_TO_DECORATOR_MAP,
)
from ._fields import (
    VALIDATOR_CONFIG_KEY as VALIDATOR_CONFIG_KEY,
)
from ._fields import (
    PydanticFieldWrapper as PydanticFieldWrapper,
)
from ._fields import (
    _all_field_arg_names as _all_field_arg_names,
)
from ._fields import (
    _change_annotated_metadata as _change_annotated_metadata,
)
from ._fields import (
    _delete_metadata_attribute as _delete_metadata_attribute,
)
from ._fields import (
    _empty_field_info as _empty_field_info,
)
from ._fields import (
    _extract_passed_field_attributes as _extract_passed_field_attributes,
)
from ._fields import (
    _field_identities as _field_identities,
)
from ._fields import (
    _FieldAttributeChange as _FieldAttributeChange,
)
from ._fields import (
    _get_aliases_derived_from_alias as _get_aliases_derived_from_alias,
)
from ._fields import (
    _get_unset_metadata_attribute_value as _get_unset_metadata_attribute_value,
)
from ._fields import (
    _has_default_and_default_factory as _has_default_and_default_factory,
)
from ._fields import (
    _keep_last_of_default_and_default_factory as _keep_last_of_default_and_default_factory,
)
from ._fields import (
    _make_derived_aliases_follow_alias as _make_derived_aliases_follow_alias,
)
from ._fields import (
    _metadata_attribute_can_be_deleted as _metadata_attribute_can_be_deleted,
)
from ._fields import (
    _metadata_has_attribute as _metadata_has_attribute,
)
from ._fields import (
    _PerFieldValidatorWrapper as _PerFieldValidatorWrapper,
)
from ._fields import (
    _sets_default_attribute as _sets_default_attribute,
)
from ._fields import (
    _ValidatorWrapper as _ValidatorWrapper,
)
from ._fields import (
    _wrap_validator as _wrap_validator,
)
from ._fields import (
    dict_of_empty_field_info as dict_of_empty_field_info,
)
from ._generics import (
    _alias_value_uses_fallbacks as _alias_value_uses_fallbacks,
)
from ._generics import (
    _change_version_of_generic_metadata as _change_version_of_generic_metadata,
)
from ._generics import (
    _eval_generic_args as _eval_generic_args,
)
from ._generics import (
    _get_typevar_uses as _get_typevar_uses,
)
from ._generics import (
    _is_generic_model as _is_generic_model,
)
from ._generics import (
    _is_parametrization as _is_parametrization,
)
from ._generics import (
    _is_same_parametrization as _is_same_parametrization,
)
from ._models import (
    _BUILTIN_CONTAINER_TYPES as _BUILTIN_CONTAINER_TYPES,
)
from ._models import (
    _PYDANTIC_CONSTRUCTORS as _PYDANTIC_CONSTRUCTORS,
)
from ._models import (
    _REJECT_KEYS_RENAMED_BY_ALIAS as _REJECT_KEYS_RENAMED_BY_ALIAS,
)
from ._models import (
    _T_COMPONENT_VALUE as _T_COMPONENT_VALUE,
)
from ._models import (
    _T_NODE as _T_NODE,
)
from ._models import (
    _T_PYDANTIC_MODEL as _T_PYDANTIC_MODEL,
)
from ._models import (
    _UNKNOWN_BASE_ADVICE as _UNKNOWN_BASE_ADVICE,
)
from ._models import (
    SchemaGenerator as SchemaGenerator,
)
from ._models import (
    _add_field_to_model as _add_field_to_model,
)
from ._models import (
    _AnnotationTransformer as _AnnotationTransformer,
)
from ._models import (
    _apply_alter_schema_instructions as _apply_alter_schema_instructions,
)
from ._models import (
    _Call as _Call,
)
from ._models import (
    _change_field as _change_field,
)
from ._models import (
    _change_field_in_model as _change_field_in_model,
)
from ._models import (
    _change_model as _change_model,
)
from ._models import (
    _change_model_config as _change_model_config,
)
from ._models import (
    _change_version_of_model_bases as _change_version_of_model_bases,
)
from ._models import (
    _change_version_of_model_references as _change_version_of_model_references,
)
from ._models import (
    _check_that_deleted_fields_are_not_inherited as _check_that_deleted_fields_are_not_inherited,
)
from ._models import (
    _check_that_renamed_fields_are_not_inherited as _check_that_renamed_fields_are_not_inherited,
)
from ._models import (
    _contains_pydantic_recursive_refs as _contains_pydantic_recursive_refs,
)
from ._models import (
    _copy_of_parent_has_field as _copy_of_parent_has_field,
)
from ._models import (
    _create_model_bundle as _create_model_bundle,
)
from ._models import (
    _CycleCopier as _CycleCopier,
)
from ._models import (
    _declares_name as _declares_name,
)
from ._models import (
    _delete_field_attributes as _delete_field_attributes,
)
from ._models import (
    _delete_field_from_model as _delete_field_from_model,
)
from ._models import (
    _DeletedField as _DeletedField,
)
from ._models import (
    _FieldName as _FieldName,
)
from ._models import (
    _FieldRename as _FieldRename,
)
from ._models import (
    _find_strongly_connected_components as _find_strongly_connected_components,
)
from ._models import (
    _format_bases as _format_bases,
)
from ._models import (
    _GenerationAnalysis as _GenerationAnalysis,
)
from ._models import (
    _get_bases_that_give_inherited_field as _get_bases_that_give_inherited_field,
)
from ._models import (
    _get_cycle_name as _get_cycle_name,
)
from ._models import (
    _get_description as _get_description,
)
from ._models import (
    _get_field_identities as _get_field_identities,
)
from ._models import (
    _get_first_renames as _get_first_renames,
)
from ._models import (
    _get_head_model_references as _get_head_model_references,
)
from ._models import (
    _get_instructions_by_model as _get_instructions_by_model,
)
from ._models import (
    _get_latest_generic_args_error as _get_latest_generic_args_error,
)
from ._models import (
    _get_models_in_field as _get_models_in_field,
)
from ._models import (
    _get_models_validated_by_fields as _get_models_validated_by_fields,
)
from ._models import (
    _get_models_with_instructions as _get_models_with_instructions,
)
from ._models import (
    _get_name_of_field as _get_name_of_field,
)
from ._models import (
    _get_parent_rename_error as _get_parent_rename_error,
)
from ._models import (
    _get_parent_renames as _get_parent_renames,
)
from ._models import (
    _get_parent_that_renamed_field as _get_parent_that_renamed_field,
)
from ._models import (
    _get_sort_key_of_model as _get_sort_key_of_model,
)
from ._models import (
    _get_subclasses_that_can_be_versioned as _get_subclasses_that_can_be_versioned,
)
from ._models import (
    _get_subclasses_that_share_field as _get_subclasses_that_share_field,
)
from ._models import (
    _is_pydantic_model_class as _is_pydantic_model_class,
)
from ._models import (
    _iter_values_within as _iter_values_within,
)
from ._models import (
    _make_subclasses_follow_rename as _make_subclasses_follow_rename,
)
from ._models import (
    _migrate_classes as _migrate_classes,
)
from ._models import (
    _ModelBundle as _ModelBundle,
)
from ._models import (
    _ModelReferences as _ModelReferences,
)
from ._models import (
    _only_changes_fields as _only_changes_fields,
)
from ._models import (
    _ParentRename as _ParentRename,
)
from ._models import (
    _PydanticModelWrapper as _PydanticModelWrapper,
)
from ._models import (
    _reference_cycle_member_ids as _reference_cycle_member_ids,
)
from ._models import (
    _ReferenceCycles as _ReferenceCycles,
)
from ._models import (
    _ReferencedModelCollector as _ReferencedModelCollector,
)
from ._models import (
    _RuntimeSchemaGenContext as _RuntimeSchemaGenContext,
)
from ._models import (
    _SchemaChanges as _SchemaChanges,
)
from ._models import (
    _set_keys_renamed_by_alias as _set_keys_renamed_by_alias,
)
from ._models import (
    _shares_attribute_objects as _shares_attribute_objects,
)
from ._models import (
    _substitute_type_vars_of_inherited_annotation as _substitute_type_vars_of_inherited_annotation,
)
from ._models import (
    _VersioningQuestion as _VersioningQuestion,
)
from ._models import (
    _wrap_pydantic_model as _wrap_pydantic_model,
)
from ._models import (
    generate_versioned_models as generate_versioned_models,
)
from ._models import (
    get_models_that_routes_request as get_models_that_routes_request,
)
from ._models import (
    migrate_response_body as migrate_response_body,
)
