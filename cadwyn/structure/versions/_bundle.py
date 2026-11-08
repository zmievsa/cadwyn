import functools
from collections.abc import (
    Callable,
    Coroutine,
    Iterator,
)
from contextlib import AsyncExitStack
from contextvars import ContextVar
from enum import Enum
from typing import (
    Any,
    Union,
)

from fastapi import BackgroundTasks
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel
from typing_extensions import ParamSpec, TypeVar

from cadwyn._utils import (
    Sentinel,
    is_enum_looked_up_by_value,
)
from cadwyn.exceptions import (
    CadwynError,
    CadwynStructureError,
)
from cadwyn.structure.common import Endpoint, VersionedModel, VersionType
from cadwyn.structure.data import (
    RequestInfo,
    ResponseInfo,
)

from ._background import _add_keyword_only_parameter, _DroppedBackgroundTasksWarning
from ._changes import HeadVersion, IdentifierPythonPath, Version, VersionChange, get_cls_pythonpath
from ._common import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME
from ._enums import _replace_with_head_enum_members
from ._forms import (
    _FormModelOmission,  # noqa: TC001 # Needed by runtime annotation introspection.
)
from ._json import _ClientJsonStrings  # noqa: TC001 # Needed by runtime annotation introspection.
from ._migration_targets import (
    _get_models_of_schema,
    _get_request_body_target,
    _get_schema_migration_target,
    _has_union_member_migrations,
    _SchemaMigrationTarget,
)
from ._requests import (
    _convert_endpoint_kwargs_to_version,
    _migrate_request,
)
from ._responses import (
    _convert_endpoint_response_to_version,
    _migrate_response,
)
from ._uploads import (
    _UploadPositions,  # noqa: TC001 # Needed by runtime annotation introspection.
)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class VersionBundle:
    def __init__(
        self,
        latest_version_or_head_version: Union[Version, HeadVersion],
        /,
        *other_versions: Version,
        api_version_var: Union[ContextVar[Union[VersionType, None]], None] = None,
    ) -> None:
        super().__init__()

        if isinstance(latest_version_or_head_version, HeadVersion):
            self.head_version = latest_version_or_head_version
            self.versions = other_versions
        else:
            self.head_version = HeadVersion()
            self.versions = (latest_version_or_head_version, *other_versions)
        self.reversed_versions = tuple(reversed(self.versions))

        if api_version_var is None:
            api_version_var = ContextVar("cadwyn_api_version")

        self.version_values = tuple(version.value for version in self.versions)
        self.reversed_version_values = tuple(reversed(self.version_values))
        self.api_version_var = api_version_var
        self._all_versions = (self.head_version, *self.versions)
        self._version_changes_to_version_mapping = {
            version_change: version.value for version in self.versions for version_change in version.changes
        }
        self._version_values_set: set[str] = set()
        for version in self.versions:
            if version.value not in self._version_values_set:
                self._version_values_set.add(version.value)
            else:
                raise CadwynStructureError(
                    f"You tried to define two versions with the same value in the same "
                    f"{VersionBundle.__name__}: '{version.value}'.",
                )
            for version_change in version.changes:
                if version_change._bound_version_bundle is not None:
                    raise CadwynStructureError(
                        f"You tried to bind version change '{version_change.__name__}' to two different versions. "
                        "It is prohibited.",
                    )
                version_change._bound_version_bundle = self
        if not self.versions:
            raise CadwynStructureError("You must define at least one non-head version in a VersionBundle.")

        if self.versions[-1].changes:
            raise CadwynStructureError(
                f'The first version "{self.versions[-1].value}" cannot have any version changes. '
                "Version changes are defined to migrate to/from a previous version so you "
                "cannot define one for the very first version.",
            )

    def __iter__(self) -> Iterator[Version]:
        yield from self.versions

    @functools.cached_property
    def versioned_schemas(self) -> dict[IdentifierPythonPath, type[VersionedModel]]:
        altered_schemas = {
            get_cls_pythonpath(instruction.schema): instruction.schema
            for version in self._all_versions
            for version_change in version.changes
            for instruction in list(version_change.alter_schema_instructions)
        }

        # Migrations for a union (e.g. `Union[A, B]`) are migrations for its members
        migrated_schemas = {
            get_cls_pythonpath(model): model
            for version in self._all_versions
            for version_change in version.changes
            for schema in list(version_change.alter_request_by_schema_instructions.keys())
            for model in _get_models_of_schema(schema)
        }

        return altered_schemas | migrated_schemas

    @functools.cached_property
    def _all_changes(self) -> tuple[type["VersionChange"], ...]:
        return tuple(version_change for version in self.versions for version_change in version.changes)

    @functools.cached_property
    def versioned_enums(self) -> dict[IdentifierPythonPath, type[Enum]]:
        return {
            get_cls_pythonpath(instruction.enum): instruction.enum
            for version in self._all_versions
            for version_change in version.changes
            for instruction in version_change.alter_enum_instructions
        }

    @functools.cached_property
    def _has_versioned_plain_enums(self) -> bool:
        """Whether a plain enum or flag has enum instructions, so that the head rejects the members of its copies.

        Pydantic looks up the members of `str`, `int`, and `float` enums by their values, so the head accepts
        the members of their copies.
        """
        return any(not is_enum_looked_up_by_value(enum) for enum in self.versioned_enums.values())

    def _replace_with_head_enum_members(self, body: Any) -> Any:
        return _replace_with_head_enum_members(body) if self._has_versioned_plain_enums else body

    def _get_closest_lesser_version(self, version: VersionType):
        for defined_version in self.version_values:
            if defined_version <= version:
                return defined_version
        raise CadwynError("You tried to migrate to version that is earlier than the first version which is prohibited.")

    async def _migrate_request(
        self,
        body_target: "_SchemaMigrationTarget",
        head_dependant: Dependant,
        request: FastapiRequest,
        response: FastapiResponse,
        request_info: RequestInfo,
        current_version: VersionType,
        head_route: APIRoute,
        *,
        route: APIRoute,
        exit_stack: AsyncExitStack,
        embed_body_fields: bool,
        background_tasks: Union[BackgroundTasks, None],
        dumped_body: Union[BaseModel, None],
        body_is_omitted: bool,
        form_model_omission: "_FormModelOmission",
        uploads_read_by_fastapi: "Union[_UploadPositions, None]",
        get_body_union_member: "Callable[[], Union[type[BaseModel], None]]",
        client_json_strings: "_ClientJsonStrings",
    ) -> dict[str, Any]:
        return await _migrate_request(
            self,
            body_target,
            head_dependant,
            request,
            response,
            request_info,
            current_version,
            head_route,
            route=route,
            exit_stack=exit_stack,
            embed_body_fields=embed_body_fields,
            background_tasks=background_tasks,
            dumped_body=dumped_body,
            body_is_omitted=body_is_omitted,
            form_model_omission=form_model_omission,
            uploads_read_by_fastapi=uploads_read_by_fastapi,
            get_body_union_member=get_body_union_member,
            client_json_strings=client_json_strings,
        )

    def _migrate_response(
        self,
        response_info: ResponseInfo,
        current_version: VersionType,
        head_route: Union[APIRoute, None],
        response_target: "_SchemaMigrationTarget",
        get_response_union_member: "Union[Callable[[], Union[type[BaseModel], None]], None]" = None,
    ) -> ResponseInfo:
        return _migrate_response(
            self,
            response_info,
            current_version,
            head_route,
            response_target,
            get_response_union_member,
        )

    # TODO (https://github.com/zmievsa/cadwyn/issues/113): Refactor this function and all functions it calls.
    def _versioned(
        self,
        head_body_field: object,
        module_body_field_name: Union[str, None],
        route: APIRoute,
        head_route: APIRoute,
        dependant_for_request_migrations: Dependant,
        *,
        request_param_name: str,
        background_tasks_param_name: Union[str, None],
        hidden_background_tasks_param_name: Union[str, None],
        response_param_name: str,
        dropped_background_tasks_warning: "_DroppedBackgroundTasksWarning",
    ) -> "Callable[[Endpoint[_P, _R]], Callable[..., Coroutine[Any, Any, _R]]]":
        request_target = _get_request_body_target(head_body_field, head_route)
        response_target = _get_schema_migration_target(head_route.response_model)
        # Routes whose union members have no migrations don't need to tell which member a body or a response is
        if not _has_union_member_migrations(
            request_target, (change.alter_request_by_schema_instructions for change in self._all_changes)
        ):
            request_target = request_target._replace(union_members=())
        if not _has_union_member_migrations(
            response_target, (change.alter_response_by_schema_instructions for change in self._all_changes)
        ):
            response_target = response_target._replace(union_members=())

        def wrapper(endpoint: "Endpoint[_P, _R]") -> "Callable[..., Coroutine[Any, Any, _R]]":
            @functools.wraps(endpoint)
            async def decorator(*args: Any, **kwargs: Any) -> _R:
                request_param: FastapiRequest = kwargs[request_param_name]
                response_param: FastapiResponse = kwargs[response_param_name]
                # FastAPI runs these background tasks after the response
                if hidden_background_tasks_param_name is not None:
                    background_tasks_of_latest_version = kwargs.pop(hidden_background_tasks_param_name)
                elif background_tasks_param_name is not None:
                    background_tasks_of_latest_version = kwargs[background_tasks_param_name]
                else:
                    # Cadwyn 8.x behavior: the endpoint doesn't declare BackgroundTasks, so the tasks that the
                    # dependencies add in the solve of the latest version are dropped
                    background_tasks_of_latest_version = BackgroundTasks()
                method = request_param.method
                response = Sentinel
                async with AsyncExitStack() as exit_stack:
                    kwargs = await self._convert_endpoint_kwargs_to_version(
                        head_body_field,
                        module_body_field_name,
                        # Dependant must be from the version of the finally migrated request,
                        # not the version of endpoint
                        dependant_for_request_migrations,
                        request_param_name,
                        kwargs,
                        response_param,
                        route,
                        head_route,
                        exit_stack=exit_stack,
                        embed_body_fields=route._embed_body_fields,
                        background_tasks=background_tasks_of_latest_version,
                        request_target=request_target,
                    )

                    response = await self._convert_endpoint_response_to_version(
                        endpoint,
                        head_route,
                        route,
                        method,
                        response_param_name,
                        kwargs,
                        response_param,
                        response_target=response_target,
                    )
                if response is Sentinel:  # pragma: no cover
                    raise CadwynError(
                        "No response object was returned. There's a high chance that the "
                        "application code is raising an exception and a dependency with yield "
                        "has a block with a bare except, or a block with except Exception, "
                        "and is not raising the exception again. Read more about it in the "
                        "docs: https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/"
                        "#dependencies-with-yield-and-except"
                    )
                # FastAPI only runs the background tasks of the dependencies if the response has no background of its
                # own. We don't warn about the tasks that dependencies add after this point, e.g. in their teardown.
                if (
                    hidden_background_tasks_param_name is None
                    and background_tasks_param_name is None
                    and background_tasks_of_latest_version.tasks
                    and not (isinstance(response, FastapiResponse) and response.background is not None)
                ):
                    await dropped_background_tasks_warning.warn_after_the_response(request_param)
                return response

            if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _CADWYN_REQUEST_PARAM_NAME, FastapiRequest)
            if response_param_name == _CADWYN_RESPONSE_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _CADWYN_RESPONSE_PARAM_NAME, FastapiResponse)
            if hidden_background_tasks_param_name is not None:
                _add_keyword_only_parameter(decorator, hidden_background_tasks_param_name, BackgroundTasks)

            return decorator

        return wrapper

    # TODO: Simplify it
    async def _convert_endpoint_response_to_version(
        self,
        func_to_get_response_from: Endpoint,
        head_route: APIRoute,
        route: APIRoute,
        method: str,
        response_param_name: str,
        kwargs: dict[str, Any],
        fastapi_response_dependency: FastapiResponse,
        *,
        response_target: "_SchemaMigrationTarget",
    ) -> Any:
        return await _convert_endpoint_response_to_version(
            self,
            func_to_get_response_from,
            head_route,
            route,
            method,
            response_param_name,
            kwargs,
            fastapi_response_dependency,
            response_target=response_target,
        )

    async def _convert_endpoint_kwargs_to_version(
        self,
        head_body_field: object,
        body_field_alias: Union[str, None],
        head_dependant: Dependant,
        request_param_name: str,
        kwargs: dict[str, Any],
        response: FastapiResponse,
        route: APIRoute,
        head_route: APIRoute,
        *,
        exit_stack: AsyncExitStack,
        embed_body_fields: bool,
        background_tasks: Union[BackgroundTasks, None],
        request_target: "_SchemaMigrationTarget",
    ) -> dict[str, Any]:
        return await _convert_endpoint_kwargs_to_version(
            self,
            head_body_field,
            body_field_alias,
            head_dependant,
            request_param_name,
            kwargs,
            response,
            route,
            head_route,
            exit_stack=exit_stack,
            embed_body_fields=embed_body_fields,
            background_tasks=background_tasks,
            request_target=request_target,
        )
