import functools
from collections.abc import (
    Callable,  # noqa: TC003 # Needed by runtime annotation introspection.
)
from contextlib import AsyncExitStack
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
)

from fastapi import BackgroundTasks, params
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.datastructures import FormData

from cadwyn._internal.context_vars import CURRENT_DEPENDENCY_SOLVER_VAR
from cadwyn.exceptions import (
    CadwynHeadRequestValidationError,
)
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import (
    RequestInfo,
)

from ._aliases import _rescue_body
from ._body_inputs import _to_head_input
from ._common import _CADWYN_REQUEST_PARAM_NAME
from ._compat import _OMITTED_BODY
from ._dependencies import _has_other_body_readers, _has_several_body_readers
from ._enums import (
    _can_body_have_lazy_iterators,
    _can_lazy_items_be_plain_enum_copies,
    _get_lazy_item_walker,
    _to_head_values,
)
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
    _migrated_body_supplies_omitted_form_model,
    _unembed_form_model_errors,
)
from ._json import (
    _ClientJsonStrings,
    _copy_decoded_json_strings,
    _encode_planned_json_values,
    _get_route_json_values_plan,
)
from ._migration_targets import (
    _UNKNOWN_BODY,
    _get_union_member_of_body,
    _no_union_member,
    _raise_unknown_union_member_of_body,
    _SchemaMigrationTarget,
)
from ._migrations import _dump_request_body, _run_request_migrations
from ._request_body import _get_body, _get_strict_content_type
from ._uploads import (
    _get_uploads_read_by_fastapi,
    _get_uploads_to_close,
    _rewind_uploads_read_by_fastapi,
    _UploadPositions,
)

if TYPE_CHECKING:
    from ._bundle import VersionBundle


async def _migrate_request(
    versions: "VersionBundle",
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
    # Migrations can put new uploads into the form, which FastAPI doesn't know about
    uploads = _get_uploads_to_close(request) if request_info._form is not None else None
    has_run_migrations = _run_request_migrations(
        versions, request_info, current_version, body_target, get_body_union_member, head_route, uploads
    )
    if has_run_migrations:
        client_json_strings.forget_shared_strings()

    request.scope["headers"] = tuple((key.encode(), value.encode()) for key, value in request_info.headers.items())
    del request._headers
    # This gives us the ability to tell the user whether cadwyn is running its dependencies or FastAPI
    CURRENT_DEPENDENCY_SOLVER_VAR.set("cadwyn")

    form_model_lookup = _FormModelLookup(head_dependant)
    # Request migrations got the members of the copies of enums, as they always did, but the head gets its own ones.
    # Each param that reads the body gets its own iterator over each lazy value, as in plain FastAPI.
    body_with_head_members = _get_head_values(versions, request_info.body, route, head_dependant, head_route)
    if request_info._form is not None:
        if uploads_read_by_fastapi:
            await _rewind_uploads_read_by_fastapi(request_info._form, uploads_read_by_fastapi)
        body_for_solving = FormData(request_info._form)
    elif (  # pragma: no cover # FastAPI >= 0.158.2
        form_model_omission is _FormModelOmission.OMITTED_NEXT_TO_OTHER_FORM_PARAMS
        and (
            body_with_head_members is None
            or not _migrated_body_supplies_omitted_form_model(
                head_dependant, head_route, body_with_head_members, embed_body_fields=embed_body_fields
            )
        )
    ):
        # The form model stays omitted, so every other param reads the form that the client would have sent
        body_for_solving = await _get_form_for_omitted_form_model(
            request, head_dependant, head_route, body_with_head_members, embed_body_fields=embed_body_fields
        )
    # A body is omitted if the request had none for FastAPI, or if a migration has set it to None
    elif request_info.body is None and (request_info._body_is_assigned or body_is_omitted):
        body_for_solving = _OMITTED_BODY
    elif (  # pragma: no cover # FastAPI >= 0.158.2
        form_model_omission is _FormModelOmission.OMITTED_NEXT_TO_OTHER_FORM_PARAMS
    ):
        # The migrated body supplies the form model. There is no model dump to restore, but the migrated body and the
        # untouched form keys must reach every reader just as they do for a supplied form model.
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
            # The models that we replace with data can have members of copies of enums and lazy values too
            _get_head_values(
                versions,
                _to_head_input(
                    route, head_dependant, head_route, request_info.body, embed_body_fields=embed_body_fields
                ),
                route,
                head_dependant,
                head_route,
            ),
            client_json_strings,
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


async def _convert_endpoint_kwargs_to_version(
    versions: "VersionBundle",
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

    api_version = versions.api_version_var.get()
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
    new_kwargs = await _migrate_request(
        versions,
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


def _get_head_values(
    versions: "VersionBundle", body: Any, route: APIRoute, head_dependant: Dependant, head_route: APIRoute
) -> Any:
    """Return the migrated body with the values that the head needs (see `_to_head_values`)"""
    replace_enum_members = versions._has_versioned_plain_enums
    # A single param can't share the iterators with anyone
    replay_lazy_iterators = _can_body_have_lazy_iterators(route, in_lazy_items=False) and _has_several_body_readers(
        head_dependant, head_route
    )
    if not replace_enum_members and not replay_lazy_iterators:
        return body
    replace_lazy_enum_members = replace_enum_members and _can_lazy_items_be_plain_enum_copies(route)
    walk_lazy_items = replace_lazy_enum_members or (
        replay_lazy_iterators and _can_body_have_lazy_iterators(route, in_lazy_items=True)
    )
    return _to_head_values(
        body,
        replace_enum_members=replace_enum_members,
        replay_lazy_iterators=replay_lazy_iterators,
        walk_lazy_item=_get_lazy_item_walker(
            replace_enum_members=replace_lazy_enum_members, replay_lazy_iterators=replay_lazy_iterators
        )
        if walk_lazy_items
        else None,
    )


async def _encode_json_values(
    request: FastapiRequest,
    head_dependant: Dependant,
    head_route: APIRoute,
    route: APIRoute,
    body: Any,
    client_json_strings: _ClientJsonStrings,
    *,
    embed_body_fields: bool,
) -> Any:
    """Return a copy of the migrated body with the parsed values of the head's `Json[...]` fields encoded as JSON.

    We dump bodies in Python mode, so `Json[...]` fields get their parsed values, which they reject because they only
    accept strings and bytes, or decode once more if they are strings. Request migrations see these parsed values,
    so we encode them only for the head. We only encode the values that we can find for sure: anything else stays as
    it is and fails as before.
    """
    plan = _get_route_json_values_plan(head_dependant, route, embed_body_fields=embed_body_fields)
    # FastAPI gives the body param its default without validating it if the request has no body, so the values of
    # the default might not have the types that the request's version validates them into
    if plan is None or await _fastapi_got_no_body(request, route, embed_body_fields=embed_body_fields):
        return body
    return _encode_planned_json_values(
        plan, body, client_json_strings, functools.partial(_has_other_body_readers, head_dependant, head_route)
    )
