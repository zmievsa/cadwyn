import functools
import http
import json
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
    cast,
)

from fastapi import BackgroundTasks, HTTPException, params
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette._utils import is_async_callable
from starlette.datastructures import FormData
from typing_extensions import ParamSpec, TypeVar

from cadwyn._internal.context_vars import CURRENT_DEPENDENCY_SOLVER_VAR
from cadwyn._utils import (
    Sentinel,
    is_enum_looked_up_by_value,
)
from cadwyn.exceptions import (
    CadwynError,
    CadwynHeadRequestValidationError,
    CadwynStructureError,
)
from cadwyn.structure.common import Endpoint, VersionedModel, VersionType
from cadwyn.structure.data import (
    RequestInfo,
    ResponseInfo,
    _BaseAlterResponseInstruction,
)

from ._aliases import _rescue_body
from ._background import _add_keyword_only_parameter, _DroppedBackgroundTasksWarning
from ._body_inputs import _to_head_input
from ._changes import HeadVersion, IdentifierPythonPath, Version, VersionChange, get_cls_pythonpath
from ._common import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME
from ._compat import _OMITTED_BODY
from ._enums import _replace_with_head_enum_members, _to_head_lazy_enum_input
from ._forms import (
    _body_is_in_kwargs,
    _embed_form_model,
    _fastapi_got_no_body,
    _FormModelLookup,
    _FormModelOmission,
    _get_form_for_omitted_form_model,
    _get_form_model_lookup,
    _get_form_model_omission,
    _get_omitted_form_model_lookup,
    _unembed_form_model_errors,
)
from ._json import _ClientJsonStrings, _copy_decoded_json_strings, _encode_json_values, _get_route_json_values_plan
from ._migration_targets import (
    _UNKNOWN_BODY,
    _get_models_of_schema,
    _get_request_body_target,
    _get_schema_migration_target,
    _get_schema_migrations,
    _get_union_member_of_body,
    _get_union_member_of_response,
    _has_union_member_migrations,
    _no_union_member,
    _raise_unknown_union_member_of_body,
    _SchemaMigrationTarget,
)
from ._requests import _dump_request_body, _get_body, _get_strict_content_type, _run_request_migration
from ._responses import _prepare_response_content, _rescue_response
from ._uploads import (
    _get_uploads_read_by_fastapi,
    _get_uploads_to_close,
    _rewind_uploads_read_by_fastapi,
    _UploadPositions,
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
        start = self.reversed_version_values.index(current_version)
        head_route_id = id(head_route)
        has_run_migrations = False
        # Migrations can put new uploads into the form, which FastAPI doesn't know about
        uploads = _get_uploads_to_close(request) if request_info._form is not None else None
        for v in self.reversed_versions[start + 1 :]:
            for version_change in v.changes:
                for instruction in _get_schema_migrations(
                    version_change.alter_request_by_schema_instructions, body_target, get_body_union_member
                ):
                    _run_request_migration(instruction, request_info, uploads)
                    has_run_migrations = True
                if head_route_id in version_change._route_to_request_migration_mapping:
                    for instruction in version_change._route_to_request_migration_mapping[head_route_id]:
                        _run_request_migration(instruction, request_info, uploads)
                        has_run_migrations = True
        if has_run_migrations:
            client_json_strings.forget_shared_strings()

        request.scope["headers"] = tuple((key.encode(), value.encode()) for key, value in request_info.headers.items())
        del request._headers
        # This gives us the ability to tell the user whether cadwyn is running its dependencies or FastAPI
        CURRENT_DEPENDENCY_SOLVER_VAR.set("cadwyn")

        form_model_lookup = _FormModelLookup(head_dependant)
        # Request migrations got the members of the copies of enums, as they always did, but the head gets its own ones
        body_with_head_members = self._replace_with_head_enum_members(request_info.body)
        if request_info._form is not None:
            if uploads_read_by_fastapi:
                await _rewind_uploads_read_by_fastapi(request_info._form, uploads_read_by_fastapi)
            body_for_solving = FormData(request_info._form)
        elif (  # pragma: no cover # FastAPI >= 0.158.2
            form_model_omission is _FormModelOmission.OMITTED_NEXT_TO_OTHER_FORM_PARAMS and request_info.body is None
        ):
            body_for_solving = await _get_form_for_omitted_form_model(request, head_dependant, head_route)
        # A body is omitted if the request had none for FastAPI, or if a migration has set it to None
        elif request_info.body is None and (request_info._body_is_assigned or body_is_omitted):
            body_for_solving = _OMITTED_BODY
        elif (  # pragma: no cover # FastAPI >= 0.158.2
            form_model_omission is _FormModelOmission.OMITTED_NEXT_TO_OTHER_FORM_PARAMS
        ):
            # There is no model dump to restore, but the migrated body and the untouched form keys must reach
            # every reader just as they do for a supplied form model.
            body_for_solving = body_with_head_members
            form_model_lookup = await _get_form_model_lookup(
                request,
                head_dependant,
                head_route,
                body_for_solving,
                None,
                embed_body_fields=embed_body_fields,
                is_migrated=has_run_migrations,
            )
        elif form_model_omission is _FormModelOmission.OMITTED:  # pragma: no cover # FastAPI >= 0.158.2
            # A migration has set the body of an omitted form model. It isn't a dump, so we don't restore its aliases
            body_for_solving = request_info.body
            form_model_lookup = _get_omitted_form_model_lookup(
                head_dependant, head_route, body_for_solving, embed_body_fields=embed_body_fields
            )
        elif dumped_body is not None:
            body_for_solving = _rescue_body(head_dependant.body_params[0], body_with_head_members)
            body_for_solving = await _encode_json_values(
                request,
                head_dependant,
                head_route,
                route,
                body_for_solving,
                client_json_strings,
                embed_body_fields=embed_body_fields,
            )
            form_model_lookup = await _get_form_model_lookup(
                request,
                head_dependant,
                head_route,
                body_for_solving,
                dumped_body,
                embed_body_fields=embed_body_fields,
                is_migrated=has_run_migrations,
            )
        else:
            body_for_solving = await _encode_json_values(
                request,
                head_dependant,
                head_route,
                route,
                # The models that we replace with data can have members of copies of enums too
                self._replace_with_head_enum_members(
                    _to_head_input(
                        route, head_dependant, head_route, request_info.body, embed_body_fields=embed_body_fields
                    )
                ),
                client_json_strings,
                embed_body_fields=embed_body_fields,
            )

        body_for_solving = _to_head_lazy_enum_input(
            body_for_solving,
            head_dependant,
            head_route,
            enabled=self._has_versioned_plain_enums and request_info._form is None,
            embed_body_fields=embed_body_fields,
        )

        # Remember this: if len(body_params) == 1, then route.body_schema == route.dependant.body_params[0]
        # We solve the head's dependencies exactly once, because solving them again would run the dependencies
        # that aren't cached, e.g. `use_cache=False` and `yield` ones, once more. If the head rejects the restored body,
        # we report its errors instead of solving again with the body as it is, which would also hide the client's
        # values behind the defaults that some fields get from their core schemas.
        result = await solve_dependencies(
            request=request,
            body=_embed_form_model(body_for_solving, form_model_lookup),
            response=response,
            dependant=form_model_lookup.dependant,
            dependency_overrides_provider=head_route.dependency_overrides_provider,
            async_exit_stack=exit_stack,
            embed_body_fields=embed_body_fields,
            background_tasks=background_tasks,
        )
        if result.errors:
            errors = _unembed_form_model_errors(result.errors, form_model_lookup.aliases)
            raise CadwynHeadRequestValidationError(errors, body=request_info.body, version=current_version)
        return result.values

    def _migrate_response(
        self,
        response_info: ResponseInfo,
        current_version: VersionType,
        head_route: Union[APIRoute, None],
        response_target: "_SchemaMigrationTarget",
        get_response_union_member: "Union[Callable[[], Union[type[BaseModel], None]], None]" = None,
    ) -> ResponseInfo:
        if get_response_union_member is None:
            get_response_union_member = _no_union_member
        head_route_id = id(head_route)
        end = self.version_values.index(current_version)
        for v in self.versions[:end]:
            for version_change in v.changes:
                migrations_to_apply: list[_BaseAlterResponseInstruction] = list(
                    _get_schema_migrations(
                        version_change.alter_response_by_schema_instructions,
                        # Route generation makes sure that the migrations of the members don't migrate HTTP errors
                        response_target
                        if response_info.status_code < 300
                        else response_target._replace(union_members=()),
                        get_response_union_member,
                    )
                )

                if head_route_id in version_change._route_to_response_migration_mapping:
                    migrations_to_apply.extend(version_change._route_to_response_migration_mapping[head_route_id])

                for migration in migrations_to_apply:
                    if response_info.status_code < 300 or migration.migrate_http_errors:
                        migration(response_info)
        return response_info

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
    async def _convert_endpoint_response_to_version(  # noqa: C901
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
        raised_exception = None
        if response_param_name == _CADWYN_RESPONSE_PARAM_NAME:
            kwargs.pop(response_param_name)
        try:
            if is_async_callable(func_to_get_response_from):
                response_or_response_body: Union[FastapiResponse, object] = await func_to_get_response_from(**kwargs)
            else:
                response_or_response_body: Union[FastapiResponse, object] = await run_in_threadpool(
                    func_to_get_response_from,
                    **kwargs,
                )
        except HTTPException as exc:
            raised_exception = exc
            response_or_response_body = FastapiResponse(
                content=json.dumps({"detail": raised_exception.detail}),
                status_code=raised_exception.status_code,
                headers=raised_exception.headers,
            )
            if (raised_exception.headers or {}).get("content-length") is None:  # pragma: no branch
                del response_or_response_body.headers["content-length"]
        api_version = self.api_version_var.get()
        if api_version is None:
            return response_or_response_body

        if isinstance(response_or_response_body, FastapiResponse):
            # TODO (https://github.com/zmievsa/cadwyn/issues/125): Add support for migrating `StreamingResponse`
            # TODO (https://github.com/zmievsa/cadwyn/issues/126): Add support for migrating `FileResponse`
            # Starlette breaks Liskov Substitution principle and
            # doesn't define `body` for `StreamingResponse` and `FileResponse`
            if isinstance(response_or_response_body, StreamingResponse | FileResponse):
                body = None
            elif response_or_response_body.body:
                if (isinstance(response_or_response_body, JSONResponse) or raised_exception is not None) and isinstance(
                    response_or_response_body.body, str | bytes
                ):
                    body = json.loads(response_or_response_body.body)
                elif isinstance(response_or_response_body.body, bytes):
                    body = response_or_response_body.body.decode(response_or_response_body.charset)
                else:  # pragma: no cover # I don't see a good use case here yet
                    body = response_or_response_body.body
            else:
                body = None
                # TODO (https://github.com/zmievsa/cadwyn/issues/51): Only do this if there are migrations

            response_info = ResponseInfo(response_or_response_body, body)
        else:
            if fastapi_response_dependency.status_code is not None:
                status_code = fastapi_response_dependency.status_code
            elif route.status_code is not None:
                status_code = route.status_code
            elif raised_exception is not None:
                raise NotImplementedError
            else:
                status_code = 200
            fastapi_response_dependency.status_code = status_code
            response_info = ResponseInfo(
                fastapi_response_dependency,
                _prepare_response_content(
                    response_or_response_body,
                    exclude_unset=head_route.response_model_exclude_unset,
                    exclude_defaults=head_route.response_model_exclude_defaults,
                    exclude_none=head_route.response_model_exclude_none,
                ),
            )

        response_info = self._migrate_response(
            response_info,
            api_version,
            head_route,
            response_target,
            (
                functools.partial(
                    _get_union_member_of_response,
                    response_or_response_body,
                    response_target.union_members,
                    route,
                    head_route,
                )
                if response_target.union_members
                else _no_union_member
            ),
        )

        if isinstance(response_or_response_body, FastapiResponse):
            # a webserver (uvicorn for instance) calculates the body at the endpoint level.
            # if an endpoint returns no "body", its content-length will be set to 0
            # json.dumps(None) results into "null", and content-length should be 4,
            # but it was already calculated to 0 which causes
            # `RuntimeError: Response content longer than Content-Length` or
            # `Too much data for declared Content-Length`, based on the protocol
            # which is why we skip the None case.

            # We skip cases without "body" attribute because of StreamingResponse and FileResponse
            # that do not have it. We don't support it too.
            if response_info.body is not None and hasattr(response_info._response, "body"):
                # TODO (https://github.com/zmievsa/cadwyn/issues/51): Only do this if there are migrations
                if (
                    isinstance(response_info.body, str)
                    and response_info._response.headers.get("content-type") != "application/json"
                ):
                    response_info._response.body = response_info.body.encode(response_info._response.charset)
                else:
                    response_info._response.body = json.dumps(
                        response_info.body,
                        ensure_ascii=False,
                        allow_nan=False,
                        indent=None,
                        separators=(",", ":"),
                    ).encode("utf-8")
                if response_info.headers.get("content-length") is not None:
                    # It makes sense to re-calculate content length because the previously calculated one
                    # might slightly differ. If it differs -- uvicorn will break.
                    response_info.headers["content-length"] = str(len(response_info._response.body))

            if raised_exception is not None and response_info.status_code >= 400:
                if isinstance(response_info.body, dict) and "detail" in response_info.body:
                    detail = response_info.body["detail"]
                else:
                    detail = response_info.body
                if detail is None:
                    detail = http.HTTPStatus(response_info.status_code).phrase
                raised_exception.detail = cast("str", detail)
                raised_exception.headers = dict(response_info.headers)
                raised_exception.status_code = response_info.status_code

                raise raised_exception
            return response_info._response
        if route.response_field is None:
            return response_info.body
        return _rescue_response(
            route.response_field, head_route.response_model, response_or_response_body, response_info.body
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
        request: FastapiRequest = kwargs[request_param_name]
        if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
            kwargs.pop(request_param_name)
        if route.body_field is not None and isinstance(route.body_field.field_info, params.Form):
            # FastAPI has already parsed this form, so this returns its cached form
            _get_uploads_to_close(request).add((await request.form()).multi_items())

        api_version = self.api_version_var.get()
        if api_version is None:
            return kwargs

        # The class of the model whose dump the body is, e.g. the member of a union that the request matches
        dumped_body: Union[BaseModel, None] = None
        form_model_omission = _FormModelOmission.NOT_OMITTED
        uploads_read_by_fastapi: Union[_UploadPositions, None] = None
        # The validated body that tells which member of a union body the request matches, if we have it
        validated_body: object = _UNKNOWN_BODY
        # The copies of the strings that the client has sent in `Json[...]` fields, by their paths in the body
        client_json_strings = _ClientJsonStrings()
        # This is a kind of body param you get when you define a single pydantic schema in your route's body
        if (
            len(route.dependant.body_params) == 1
            and head_body_field is not None
            and body_field_alias is not None
            and _body_is_in_kwargs(route, body_field_alias, embed_body_fields=embed_body_fields)
        ):
            # FastAPI keys the kwargs by param names, so a kwarg named after the body's alias can be another param
            raw_body: Union[BaseModel, None] = kwargs[route.dependant.body_params[0].name]
            validated_body = raw_body
            if raw_body is None:
                body = None
                form_model_omission = await _get_form_model_omission(request, route)
            # It means we have a dict or a list instead of a full model.
            # This covers the following use case in the endpoint definition: "payload: dict = Body(None)"
            elif not isinstance(raw_body, BaseModel):
                body = raw_body
            else:
                body = _dump_request_body(raw_body)
                dumped_body = raw_body
            if body is not None:
                plan = _get_route_json_values_plan(head_dependant, route, embed_body_fields=embed_body_fields)
                body = _copy_decoded_json_strings(plan, body, client_json_strings)
        else:
            # This is for requests without body or with complex body such as form or file
            body = await _get_body(request, route.body_field, strict_content_type=_get_strict_content_type(request))
            if isinstance(body, FormData):
                uploads_read_by_fastapi = _get_uploads_read_by_fastapi(route, head_route, head_dependant, body)

        body_is_omitted = body is None and (
            form_model_omission is _FormModelOmission.OMITTED
            or await _fastapi_got_no_body(request, route, embed_body_fields=embed_body_fields)
        )
        request_info = RequestInfo(request, body)
        if not request_target.union_members:
            get_body_union_member = _no_union_member
        elif validated_body is _UNKNOWN_BODY:
            get_body_union_member = functools.partial(_raise_unknown_union_member_of_body, route, body_field_alias)
        else:
            get_body_union_member = functools.partial(
                _get_union_member_of_body, validated_body, request_target.union_members, head_body_field
            )
        new_kwargs = await self._migrate_request(
            request_target,
            head_dependant,
            request,
            response,
            request_info,
            api_version,
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
        # Because we re-added it into our kwargs when we did solve_dependencies
        if _CADWYN_REQUEST_PARAM_NAME in new_kwargs:
            new_kwargs.pop(_CADWYN_REQUEST_PARAM_NAME)

        return new_kwargs
