import functools
import http
import json
from collections.abc import (
    Callable,  # noqa: TC003 # Needed by runtime annotation introspection.
)
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    cast,
)

from fastapi import HTTPException
from fastapi import Response as FastapiResponse
from fastapi._compat import (
    ModelField,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette._utils import is_async_callable

from cadwyn.structure.common import Endpoint, VersionType
from cadwyn.structure.data import (
    ResponseInfo,
    _BaseAlterResponseInstruction,
)

from ._aliases import _get_restorer_spec, _is_dumped_from_models, _rescue_body
from ._common import _CADWYN_RESPONSE_PARAM_NAME
from ._migration_targets import (
    _get_schema_migrations,
    _get_union_member_of_response,
    _no_union_member,
    _SchemaMigrationTarget,
)

if TYPE_CHECKING:
    from ._bundle import VersionBundle


def _prepare_response_content(
    res: Any,
    *,
    exclude_unset: bool,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
) -> Any:
    """Serialize Pydantic models to dicts for response processing.

    It is much easier to alter dicts and lists than Pydantic models in request/response migrations
    """
    if isinstance(res, BaseModel):
        return res.model_dump(
            by_alias=True,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )
    elif isinstance(res, list):
        return [
            _prepare_response_content(
                item,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
            )
            for item in res
        ]
    elif isinstance(res, dict):
        return {
            k: _prepare_response_content(
                v,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
            )
            for k, v in res.items()
        }
    return res


def _rescue_response(response_field: ModelField, head_response_model: object, content: Any, data: Any) -> Any:
    """Rescue the migrated `_prepare_response_content` of `content` if it surely fails validation due to dumped keys.

    That's the case when a required field, or a model that forbids extra keys, gets a dumped key instead of
    a validation key. We restore its validation aliases and let FastAPI validate the restored response, so its
    validators run once, like in FastAPI without Cadwyn. The response fails as it is anyway, or silently gets
    the defaults that the core schemas of the types of some fields give them (see `_get_failing_keys`), so if
    the restored response fails, FastAPI raises its errors. Otherwise, FastAPI validates the response as it is,
    as it always did, and responses that pass stay exactly as they are, even if a field gets its default instead
    of its value.
    """
    restored = _rescue_body(response_field, data)
    if restored is data:
        return data
    # We only restore the dumps of the models of the head response model that the endpoint returned. FastAPI validates
    # the dicts that the endpoint returns as they are, and the dump of another model can have the dumped keys of
    # a response model with values that it doesn't accept.
    head_spec = _get_restorer_spec(head_response_model)
    if head_spec is None or not _is_dumped_from_models(content, head_spec):
        return data
    return restored


def _migrate_response(
    versions: "VersionBundle",
    response_info: ResponseInfo,
    current_version: VersionType,
    head_route: Union[APIRoute, None],
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


async def _convert_endpoint_response_to_version(  # noqa: C901
    versions: "VersionBundle",
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
    api_version = versions.api_version_var.get()
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

    response_info = versions._migrate_response(
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
