"""The loops that run data migrations, which routes and standalone use share.

Routes and `migrate_request_body()` / `migrate_response_body()` run the same migrations by schema through these, so
the migrations give the same body either way. Only routes run migrations by path.
"""

from collections.abc import (
    Callable,  # Needed by runtime annotation introspection.
)
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
)

from pydantic import BaseModel

from cadwyn.structure.common import VersionType
from cadwyn.structure.data import (
    RequestInfo,
    ResponseInfo,
    _BaseAlterResponseInstruction,
)

from ._migration_targets import (
    _get_schema_migrations,
    _no_union_member,
    _SchemaMigrationTarget,
)

if TYPE_CHECKING:
    from fastapi.routing import APIRoute

    from ._bundle import VersionBundle
    from ._uploads import _UploadsToClose


def _dump_request_body(body: BaseModel) -> dict[str, Any]:
    """Return the data that the client has sent for a model body, without the values of its computed fields.

    The head would take them as input: as extra keys, as a 500 for models that forbid extra keys, or as the values of
    the fields that they share a key with. Like FastAPI, the head computes them itself. We can't tell whether an
    override of `model_dump()` takes `exclude_computed_fields` (e.g. the one of SQLModel before 0.0.26 doesn't), so we
    dump such models as before.
    """
    if type(body).model_dump is BaseModel.model_dump:
        return body.model_dump(by_alias=True, exclude_unset=True, exclude_computed_fields=True)
    return body.model_dump(by_alias=True, exclude_unset=True)


def _run_request_migration(
    migration: Callable[[RequestInfo], None], request_info: RequestInfo, uploads: "Union[_UploadsToClose, None]"
) -> None:
    try:
        migration(request_info)
    finally:
        # A later migration can replace or remove the uploads that this one has put into the form
        if uploads is not None and request_info._form is not None:
            uploads.add(request_info._form)


def _run_request_migrations(
    versions: "VersionBundle",
    request_info: RequestInfo,
    current_version: VersionType,
    body_target: "_SchemaMigrationTarget",
    get_body_union_member: "Callable[[], Union[type[BaseModel], None]]",
    head_route: "Union[APIRoute, None]",
    uploads: "Union[_UploadsToClose, None]",
) -> bool:
    """Run the request migrations from the request's version up to head, and return whether any of them has run.

    Routes and `migrate_request_body()` both migrate requests with it. Without a route, only migrations by schema run.
    """
    start = versions.reversed_version_values.index(current_version)
    has_run_migrations = False
    for v in versions.reversed_versions[start + 1 :]:
        for version_change in v.changes:
            for instruction in _get_schema_migrations(
                version_change.alter_request_by_schema_instructions, body_target, get_body_union_member
            ):
                _run_request_migration(instruction, request_info, uploads)
                has_run_migrations = True
            if head_route is not None and id(head_route) in version_change._route_to_request_migration_mapping:
                for instruction in version_change._route_to_request_migration_mapping[id(head_route)]:
                    _run_request_migration(instruction, request_info, uploads)
                    has_run_migrations = True
    return has_run_migrations


def _migrate_response(
    versions: "VersionBundle",
    response_info: ResponseInfo,
    current_version: VersionType,
    head_route: "Union[APIRoute, None]",
    response_target: "_SchemaMigrationTarget",
    get_response_union_member: "Union[Callable[[], Union[type[BaseModel], None]], None]" = None,
) -> ResponseInfo:
    if get_response_union_member is None:
        get_response_union_member = _no_union_member
    head_route_id = id(head_route)
    end = versions.version_values.index(current_version)
    for v in versions.versions[:end]:
        for version_change in v.changes:
            migrations_to_apply: list[_BaseAlterResponseInstruction] = list(
                _get_schema_migrations(
                    version_change.alter_response_by_schema_instructions,
                    # Route generation makes sure that the migrations of the members don't migrate HTTP errors
                    response_target if response_info.status_code < 300 else response_target._replace(union_members=()),
                    get_response_union_member,
                )
            )

            if head_route_id in version_change._route_to_response_migration_mapping:
                migrations_to_apply.extend(version_change._route_to_response_migration_mapping[head_route_id])

            for migration in migrations_to_apply:
                if response_info.status_code < 300 or migration.migrate_http_errors:
                    migration(response_info)
    return response_info
