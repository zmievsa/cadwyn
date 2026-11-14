"""The wrapper that migrates the requests and responses of the endpoints of a version's routes"""

import functools
from collections.abc import (
    Callable,  # noqa: TC003 # Needed by runtime annotation introspection.
    Coroutine,  # noqa: TC003 # Needed by runtime annotation introspection.
)
from contextlib import AsyncExitStack
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
)

from fastapi import BackgroundTasks
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from typing_extensions import ParamSpec, TypeVar

from cadwyn._utils import Sentinel
from cadwyn.exceptions import CadwynError
from cadwyn.structure.common import Endpoint  # noqa: TC001 # Needed by runtime annotation introspection.

from ._background import _add_keyword_only_parameter, _DroppedBackgroundTasksWarning
from ._common import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME
from ._migration_targets import _get_request_body_target, _get_schema_migration_target, _has_union_member_migrations
from ._requests import _convert_endpoint_kwargs_to_version
from ._responses import _convert_endpoint_response_to_version

if TYPE_CHECKING:
    from ._bundle import VersionBundle

_P = ParamSpec("_P")
_R = TypeVar("_R")


# TODO (https://github.com/zmievsa/cadwyn/issues/113): Refactor this function and all functions it calls.
def _versioned(
    versions: "VersionBundle",
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
        request_target, (change.alter_request_by_schema_instructions for change in versions._all_changes)
    ):
        request_target = request_target._replace(union_members=())
    if not _has_union_member_migrations(
        response_target, (change.alter_response_by_schema_instructions for change in versions._all_changes)
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
                kwargs = await _convert_endpoint_kwargs_to_version(
                    versions,
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

                response = await _convert_endpoint_response_to_version(
                    versions,
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
