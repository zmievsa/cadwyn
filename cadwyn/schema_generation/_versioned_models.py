import collections.abc  # noqa: TC003 # Needed by runtime annotation introspection.
import copy
from datetime import date
from enum import Enum
from functools import cache
from typing import (
    Any,
    Union,
)

import fastapi.params
import pydantic
from fastapi import Response
from pydantic import BaseModel
from typing_extensions import TypeVar

from cadwyn.exceptions import CadwynError, CadwynHeadRequestValidationError
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import ResponseInfo, _get_body_only_request_info
from cadwyn.structure.schemas import (
    SchemaHadInstruction,
)
from cadwyn.structure.versions import (  # noqa: TC001 # Needed by runtime annotation introspection.
    HeadVersion,
    Version,
    VersionBundle,
)
from cadwyn.structure.versions._aliases import _rescue_model_data
from cadwyn.structure.versions._enums import _get_lazy_item_walker, _to_head_values
from cadwyn.structure.versions._json import (
    _ClientJsonStrings,
    _copy_decoded_json_strings,
    _encode_planned_json_values,
    _get_json_values_plan,
)
from cadwyn.structure.versions._migration_targets import _get_schema_migration_target, _no_union_member
from cadwyn.structure.versions._migrations import _dump_request_body, _run_request_migrations

from ._annotation_transformer import (
    _ReferencedModelCollector,
)
from ._enums import (
    _EnumWrapper,
)
from ._instructions import (
    _check_that_deleted_fields_are_not_inherited,
    _check_that_renamed_fields_are_not_inherited,
    _get_field_identities,
    _get_first_renames,
    _get_parent_renames,
    _migrate_classes,
    _ParentRename,
)
from ._models import (
    _wrap_pydantic_model,
)
from ._references import (
    _ReferenceCycles,
)
from ._schema_generator import (
    SchemaGenerator,
    _ModelBundle,
    _RuntimeSchemaGenContext,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def migrate_request_body(
    versions: "VersionBundle",
    latest_request_model: type[_ModelT],
    *,
    body: Any,
    version: Union[VersionType, date],
) -> _ModelT:
    """Migrate the body of a request from a version up to head

    Validate the body with the passed version of latest_request_model, apply all version changes from that version
    up to head, and validate the result with latest_request_model, as a route does before it calls its endpoint
    """
    version = _get_version_to_migrate(versions, version)
    versioned_request_model = generate_versioned_models(versions)[version][latest_request_model]
    # Like routes, we migrate the data that the client has sent, so the body has the keys of
    # `model_dump(by_alias=True)` and only the fields that the client has set
    validated_body = _validate_request_body(versioned_request_model, body)
    json_values_plan = _get_json_values_plan(latest_request_model, versioned_request_model)
    client_json_strings = _ClientJsonStrings()
    request_info = _get_body_only_request_info(
        _copy_decoded_json_strings(json_values_plan, _dump_request_body(validated_body), client_json_strings)
    )
    has_run_migrations = _run_request_migrations(
        versions,
        request_info,
        version,
        _get_schema_migration_target(latest_request_model),
        _no_union_member,
        head_route=None,
        uploads=None,
    )
    if has_run_migrations:
        client_json_strings.forget_shared_strings()
    # A migration that sets the body to None omits it, which a route rejects because the head requires the body
    if request_info.body is None and request_info._body_is_assigned:
        raise CadwynHeadRequestValidationError(
            [{"type": "missing", "loc": (), "msg": "Field required", "input": None}], body=None, version=version
        )
    replace_enum_members = versions._has_versioned_plain_enums
    migrated_body = _to_head_values(
        request_info.body,
        replace_enum_members=replace_enum_members,
        replay_lazy_iterators=False,
        walk_lazy_item=_get_lazy_item_walker(replace_enum_members=True, replay_lazy_iterators=False)
        if replace_enum_members
        else None,
    )
    migrated_body = _rescue_model_data(latest_request_model, migrated_body)
    if json_values_plan is not None:
        migrated_body = _encode_planned_json_values(
            json_values_plan, migrated_body, client_json_strings, has_other_body_readers=lambda: False
        )
    try:
        return _validate_request_body(latest_request_model, migrated_body)
    except pydantic.ValidationError as exc:
        raise CadwynHeadRequestValidationError(exc.errors(), body=request_info.body, version=version) from exc


def _validate_request_body(model: type[_ModelT], body: Any) -> _ModelT:
    """Validate a request body as FastAPI does: by the attributes of objects too, and without `model_validate()`"""
    return model.__pydantic_validator__.validate_python(body, from_attributes=True)


def migrate_response_body(
    versions: "VersionBundle",
    latest_response_model: type[pydantic.BaseModel],
    *,
    latest_body: Any,
    version: Union[VersionType, date],
) -> Any:
    """Convert the data to a specific version

    Apply all version changes from latest until the passed version in reverse order
    and wrap the result in the correct version of latest_response_model
    """
    version = _get_version_to_migrate(versions, version)
    response = ResponseInfo(Response(status_code=200), body=latest_body)
    migrated_response = versions._migrate_response(
        response,
        current_version=version,
        head_route=None,
        response_target=_get_schema_migration_target(latest_response_model),
    )

    versioned_response_model: type[pydantic.BaseModel] = generate_versioned_models(versions)[str(version)][
        latest_response_model
    ]
    # The body has the keys of `model_dump(by_alias=True)`, like the bodies that routes migrate. Like routes, we only
    # restore its validation aliases if it surely fails as it is, and we validate it once, whichever body it is,
    # because validators can have side effects.
    return versioned_response_model.model_validate(_rescue_model_data(versioned_response_model, migrated_response.body))


def _get_version_to_migrate(versions: "VersionBundle", version: Union[VersionType, date]) -> VersionType:
    """Return the version of the bundle that a body of this version or date has"""
    if isinstance(version, date):
        version = version.isoformat()
        version = versions._get_closest_lesser_version(version)
    if version not in versions._version_values_set:
        raise CadwynError(f"Version {version} not found in version bundle")
    return version


def get_models_that_routes_request(routers: "collections.abc.Iterable[fastapi.routing.APIRouter]") -> set[type[Any]]:
    """Return the models that versioning the routes of the routers requests from a generator directly"""
    collector = _ReferencedModelCollector()
    for router in routers:
        for route in router.routes:
            if isinstance(route, fastapi.routing.APIRoute):
                collector.annotation_transformer.record_route(route)
    return collector.models


@cache
def generate_versioned_models(versions: "VersionBundle") -> "dict[str, SchemaGenerator]":
    models = _create_model_bundle(versions)
    changed_models = frozenset[type[Union[BaseModel, Enum]]]().union(
        *(_get_models_with_instructions(version) for version in versions._all_versions)
    )

    version_to_context_map = {}
    reference_cycles = _ReferenceCycles(changed_models)
    context = _RuntimeSchemaGenContext(current_version=versions.head_version, models=models)
    field_identities_before_version = _get_field_identities(models.schemas)
    changes = _migrate_classes(context)
    first_renames = _get_first_renames(changes.renames)
    # The renames of the newer versions, which every older version must not undo by inheriting the old field
    renames_of_newer_versions = list(first_renames)
    parent_renames_of_newer_versions: list[_ParentRename] = []
    instructions_that_differ_from_head = _get_instructions_by_model(versions.head_version, {})

    for version in versions.versions:
        context = _RuntimeSchemaGenContext(current_version=version, models=models)
        parent_renames = _get_parent_renames(first_renames, field_identities_before_version)
        generator = SchemaGenerator(
            copy.deepcopy(models),
            changed_models,
            reference_cycles,
            instructions_that_differ_from_head,
            (*parent_renames_of_newer_versions, *parent_renames),
            str(version.value),
        )
        _check_that_renamed_fields_are_not_inherited(
            renames_of_newer_versions, models.schemas, generator, str(version.value)
        )
        _check_that_deleted_fields_are_not_inherited(changes.deleted_fields, generator, str(version.value))
        version_to_context_map[str(version.value)] = generator
        parent_renames_of_newer_versions += parent_renames
        # note that the last migration will not contain any version changes so we don't need to save the results
        field_identities_before_version = _get_field_identities(models.schemas)
        changes = _migrate_classes(context)
        first_renames = _get_first_renames(changes.renames)
        renames_of_newer_versions += first_renames
        instructions_that_differ_from_head = _get_instructions_by_model(version, instructions_that_differ_from_head)

    return version_to_context_map


def _get_instructions_by_model(
    version: "Union[Version, HeadVersion]",
    instructions_of_newer_versions: "collections.abc.Mapping[type[Union[BaseModel, Enum]], tuple[Any, ...]]",
) -> "dict[type[Union[BaseModel, Enum]], tuple[Any, ...]]":
    instructions_by_model = dict(instructions_of_newer_versions)
    for version_change in version.changes:
        instructions = [
            *((instruction.schema, instruction) for instruction in version_change.alter_schema_instructions),
            *((instruction.enum, instruction) for instruction in version_change.alter_enum_instructions),
        ]
        for model, instruction in instructions:
            instructions_by_model[model] = (*instructions_by_model.get(model, ()), instruction)
    return instructions_by_model


def _get_models_with_instructions(version: "Union[Version, HeadVersion]") -> "frozenset[type[Union[BaseModel, Enum]]]":
    return frozenset(
        instruction.schema
        for version_change in version.changes
        for instruction in version_change.alter_schema_instructions
    ) | frozenset(
        instruction.enum for version_change in version.changes for instruction in version_change.alter_enum_instructions
    )


def _create_model_bundle(versions: "VersionBundle"):
    return _ModelBundle(
        enums={enum: _EnumWrapper(enum) for enum in versions.versioned_enums.values()},
        schemas={schema: _wrap_pydantic_model(schema) for schema in versions.versioned_schemas.values()},
        models_with_config_instructions=frozenset(
            instruction.schema
            for version in versions._all_versions
            for change in version.changes
            for instruction in change.alter_schema_instructions
            if isinstance(instruction, SchemaHadInstruction) and instruction.config
        ),
    )
