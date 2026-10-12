import dataclasses
import email.message
import functools
import http
import inspect
import json
from collections import defaultdict
from collections.abc import Callable, Coroutine, Iterator, Sequence
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Annotated, ClassVar, TypeAlias, Union, cast, get_args, get_origin

from fastapi import BackgroundTasks, HTTPException, params
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi._compat import ModelField
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import AliasPath, BaseModel, RootModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from starlette._utils import is_async_callable
from starlette.datastructures import FormData
from typing_extensions import Any, ParamSpec, TypeVar, assert_never, deprecated, override

from cadwyn._internal.context_vars import CURRENT_DEPENDENCY_SOLVER_VAR
from cadwyn._utils import UnionType, classproperty, lenient_issubclass
from cadwyn.exceptions import (
    CadwynError,
    CadwynHeadRequestValidationError,
    CadwynStructureError,
)

from .._utils import Sentinel
from .common import Endpoint, VersionedModel, VersionType
from .data import (
    RequestInfo,
    ResponseInfo,
    _AlterRequestByPathInstruction,
    _AlterRequestBySchemaInstruction,
    _AlterResponseByPathInstruction,
    _AlterResponseBySchemaInstruction,
    _BaseAlterResponseInstruction,
)
from .endpoints import AlterEndpointSubInstruction
from .enums import AlterEnumSubInstruction
from .schemas import AlterSchemaSubInstruction, SchemaHadInstruction

_CADWYN_REQUEST_PARAM_NAME = "cadwyn_request_param"
_CADWYN_RESPONSE_PARAM_NAME = "cadwyn_response_param"
_P = ParamSpec("_P")
_R = TypeVar("_R")
_RouteId = int

# ty requires staticmethod's type arguments, but runtime isinstance() requires the bare class.
if TYPE_CHECKING:
    _StaticMethodInstruction: TypeAlias = staticmethod[..., object]
else:
    _StaticMethodInstruction = staticmethod

PossibleInstructions: TypeAlias = Union[
    AlterSchemaSubInstruction,
    AlterEndpointSubInstruction,
    AlterEnumSubInstruction,
    SchemaHadInstruction,
    _StaticMethodInstruction,
]


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


APIVersionVarType: TypeAlias = Union[ContextVar[Union[VersionType, None]], ContextVar[VersionType]]
IdentifierPythonPath = str


if TYPE_CHECKING:
    _AlterSchemaSubInstructionArgs = AlterSchemaSubInstruction
    _AlterEnumSubInstructionArgs = AlterEnumSubInstruction
    _AlterEndpointSubInstructionArgs = AlterEndpointSubInstruction
else:
    _AlterSchemaSubInstructionArgs = get_args(AlterSchemaSubInstruction)
    _AlterEnumSubInstructionArgs = get_args(AlterEnumSubInstruction)
    _AlterEndpointSubInstructionArgs = get_args(AlterEndpointSubInstruction)


class VersionChange:
    description: ClassVar[str] = Sentinel
    is_hidden_from_changelog: ClassVar[bool] = False
    instructions_to_migrate_to_previous_version: ClassVar[Sequence[PossibleInstructions]] = Sentinel
    alter_schema_instructions: ClassVar[list[Union[AlterSchemaSubInstruction, SchemaHadInstruction]]] = Sentinel
    alter_enum_instructions: ClassVar[list[AlterEnumSubInstruction]] = Sentinel
    alter_endpoint_instructions: ClassVar[list[AlterEndpointSubInstruction]] = Sentinel
    alter_request_by_schema_instructions: ClassVar[dict[type[BaseModel], list[_AlterRequestBySchemaInstruction]]] = (
        Sentinel
    )
    alter_request_by_path_instructions: ClassVar[dict[str, list[_AlterRequestByPathInstruction]]] = Sentinel
    alter_response_by_schema_instructions: ClassVar[dict[type, list[_AlterResponseBySchemaInstruction]]] = Sentinel
    alter_response_by_path_instructions: ClassVar[dict[str, list[_AlterResponseByPathInstruction]]] = Sentinel
    _bound_version_bundle: "Union[VersionBundle, None]"
    _route_to_request_migration_mapping: ClassVar[dict[_RouteId, list[_AlterRequestByPathInstruction]]] = Sentinel
    _route_to_response_migration_mapping: ClassVar[dict[_RouteId, list[_AlterResponseByPathInstruction]]] = Sentinel

    def __init_subclass__(cls, _abstract: bool = False) -> None:
        super().__init_subclass__()

        if _abstract:
            return
        if cls.description is Sentinel and cls.__doc__:
            cls.description = cls.__doc__
        cls._validate_subclass()
        cls._extract_list_instructions_into_correct_containers()
        cls._extract_body_instructions_into_correct_containers()
        cls._check_no_subclassing()
        cls._bound_version_bundle = None
        cls._route_to_request_migration_mapping = defaultdict(list)
        cls._route_to_response_migration_mapping = defaultdict(list)

    @classmethod
    def _extract_body_instructions_into_correct_containers(cls) -> None:
        for instruction in cls.__dict__.values():
            if isinstance(instruction, _AlterRequestBySchemaInstruction):
                for schema in instruction.schemas:
                    cls.alter_request_by_schema_instructions[schema].append(instruction)
            elif isinstance(instruction, _AlterRequestByPathInstruction):
                cls.alter_request_by_path_instructions[instruction.path].append(instruction)
            elif isinstance(instruction, _AlterResponseBySchemaInstruction):
                for schema in instruction.schemas:
                    cls.alter_response_by_schema_instructions[schema].append(instruction)
            elif isinstance(instruction, _AlterResponseByPathInstruction):
                cls.alter_response_by_path_instructions[instruction.path].append(instruction)

    @classmethod
    def _extract_list_instructions_into_correct_containers(cls) -> None:
        cls.alter_schema_instructions = []
        cls.alter_enum_instructions = []
        cls.alter_endpoint_instructions = []
        cls.alter_request_by_schema_instructions = defaultdict(list)
        cls.alter_request_by_path_instructions = defaultdict(list)
        cls.alter_response_by_schema_instructions = defaultdict(list)
        cls.alter_response_by_path_instructions = defaultdict(list)
        for alter_instruction in cls.instructions_to_migrate_to_previous_version:
            if isinstance(alter_instruction, SchemaHadInstruction) or isinstance(  # noqa: SIM101
                alter_instruction, _AlterSchemaSubInstructionArgs
            ):
                cls.alter_schema_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, _AlterEnumSubInstructionArgs):
                cls.alter_enum_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, _AlterEndpointSubInstructionArgs):
                cls.alter_endpoint_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, staticmethod):  # pragma: no cover
                raise NotImplementedError(f'"{alter_instruction}" is an unacceptable version change instruction')
            else:
                assert_never(alter_instruction)

    @classmethod
    def _validate_subclass(cls) -> None:
        if cls.description is Sentinel:
            raise CadwynStructureError(
                f"Version change description is not set on '{cls.__name__}' but is required.",
            )
        if cls.instructions_to_migrate_to_previous_version is Sentinel:
            raise CadwynStructureError(
                f"Attribute 'instructions_to_migrate_to_previous_version' is not set on '{cls.__name__}'"
                " but is required.",
            )
        if not isinstance(cls.instructions_to_migrate_to_previous_version, Sequence):
            raise CadwynStructureError(
                f"Attribute 'instructions_to_migrate_to_previous_version' must be a sequence in '{cls.__name__}'.",
            )
        for instruction in cls.instructions_to_migrate_to_previous_version:
            if not isinstance(instruction, get_args(PossibleInstructions)):
                raise CadwynStructureError(
                    f"Instruction '{instruction}' is not allowed. Please, use the correct instruction types",
                )
        for attr_name, attr_value in cls.__dict__.items():
            if not isinstance(
                attr_value,
                _AlterRequestBySchemaInstruction
                | _AlterRequestByPathInstruction
                | _AlterResponseBySchemaInstruction
                | _AlterResponseByPathInstruction,
            ) and attr_name not in {
                "description",
                "side_effects",
                "instructions_to_migrate_to_previous_version",
                "__module__",
                "__doc__",
                "__firstlineno__",
                "__static_attributes__",
            }:
                raise CadwynStructureError(
                    f"Found: '{attr_name}' attribute of type '{type(attr_value)}' in '{cls.__name__}'."
                    " Only migration instructions and schema properties are allowed in version change class body.",
                )

    @classmethod
    def _check_no_subclassing(cls) -> None:
        if cls.mro() != [cls, VersionChange, object]:
            raise TypeError(
                f"Can't subclass {cls.__name__} as it was never meant to be subclassed.",
            )

    def __init__(self) -> None:
        raise TypeError(
            f"Can't instantiate {self.__class__.__name__} as it was never meant to be instantiated.",
        )


class VersionChangeWithSideEffects(VersionChange, _abstract=True):
    @classmethod
    @override
    def _check_no_subclassing(cls) -> None:
        if cls.mro() != [cls, VersionChangeWithSideEffects, VersionChange, object]:
            raise TypeError(
                f"Can't subclass {cls.__name__} as it was never meant to be subclassed.",
            )

    @classproperty
    def is_applied(cls: type["VersionChangeWithSideEffects"]) -> bool:
        if (
            cls._bound_version_bundle is None
            or cls not in cls._bound_version_bundle._version_changes_to_version_mapping
        ):
            raise CadwynError(
                f"You tried to check whether '{cls.__name__}' is active but it was never bound to any version.",
            )
        api_version = cls._bound_version_bundle.api_version_var.get()
        if api_version is None:
            return True
        return cls._bound_version_bundle._version_changes_to_version_mapping[cls] <= api_version


class Version:
    def __init__(self, value: Union[str, date], *changes: type[VersionChange]) -> None:
        super().__init__()

        if isinstance(value, date):
            value = value.isoformat()
        self.value = value
        self.changes = changes

    @property
    @deprecated("'version_changes' attribute is deprecated and will be removed in Cadwyn 5.x.x. Use 'changes' instead.")
    def version_changes(self) -> tuple[type[VersionChange], ...]:  # pragma: no cover
        return self.changes

    @override
    def __repr__(self) -> str:
        return f"Version('{self.value}')"


class HeadVersion:
    def __init__(self, *changes: type[VersionChange]) -> None:
        super().__init__()
        self.changes = changes

        for version_change in changes:
            if any(
                [
                    version_change.alter_request_by_path_instructions,
                    version_change.alter_request_by_schema_instructions,
                    version_change.alter_response_by_path_instructions,
                    version_change.alter_response_by_schema_instructions,
                ]
            ):
                raise NotImplementedError(
                    f"HeadVersion does not support request or response migrations but {version_change} contained one."
                )

    @property
    @deprecated("'version_changes' attribute is deprecated and will be removed in Cadwyn 5.x.x. Use 'changes' instead.")
    def version_changes(self) -> tuple[type[VersionChange], ...]:  # pragma: no cover
        return self.changes


def get_cls_pythonpath(cls: type) -> IdentifierPythonPath:
    return f"{cls.__module__}.{cls.__name__}"


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

    @property
    @deprecated("Use 'version_values' instead.")
    def version_dates(self) -> tuple[str, ...]:  # pragma: no cover
        return self.version_values

    @functools.cached_property
    def versioned_schemas(self) -> dict[IdentifierPythonPath, type[VersionedModel]]:
        altered_schemas = {
            get_cls_pythonpath(instruction.schema): instruction.schema
            for version in self._all_versions
            for version_change in version.changes
            for instruction in list(version_change.alter_schema_instructions)
        }

        migrated_schemas = {
            get_cls_pythonpath(schema): schema
            for version in self._all_versions
            for version_change in version.changes
            for schema in list(version_change.alter_request_by_schema_instructions.keys())
        }

        return altered_schemas | migrated_schemas

    @functools.cached_property
    def versioned_enums(self) -> dict[IdentifierPythonPath, type[Enum]]:
        return {
            get_cls_pythonpath(instruction.enum): instruction.enum
            for version in self._all_versions
            for version_change in version.changes
            for instruction in version_change.alter_enum_instructions
        }

    def _get_closest_lesser_version(self, version: VersionType):
        for defined_version in self.version_values:
            if defined_version <= version:
                return defined_version
        raise CadwynError("You tried to migrate to version that is earlier than the first version which is prohibited.")

    async def _migrate_request(
        self,
        body_type: object,
        head_dependant: Dependant,
        request: FastapiRequest,
        response: FastapiResponse,
        request_info: RequestInfo,
        current_version: VersionType,
        head_route: APIRoute,
        *,
        exit_stack: AsyncExitStack,
        embed_body_fields: bool,
        background_tasks: Union[BackgroundTasks, None],
        body_is_model_dump: bool,
    ) -> dict[str, Any]:
        start = self.reversed_version_values.index(current_version)
        head_route_id = id(head_route)
        body_schema_key = _get_schema_migration_key(body_type)
        for v in self.reversed_versions[start + 1 :]:
            for version_change in v.changes:
                if (
                    body_schema_key is not None
                    and body_schema_key in version_change.alter_request_by_schema_instructions
                ):
                    # Membership establishes that this annotation is a registered BaseModel key.
                    schema = cast("type[BaseModel]", body_schema_key)
                    for instruction in version_change.alter_request_by_schema_instructions[schema]:
                        instruction(request_info)
                if head_route_id in version_change._route_to_request_migration_mapping:
                    for instruction in version_change._route_to_request_migration_mapping[head_route_id]:
                        instruction(request_info)

        request.scope["headers"] = tuple((key.encode(), value.encode()) for key, value in request_info.headers.items())
        del request._headers
        # This gives us the ability to tell the user whether cadwyn is running its dependencies or FastAPI
        CURRENT_DEPENDENCY_SOLVER_VAR.set("cadwyn")

        if request_info._form is not None:
            body_for_solving = FormData(request_info._form)
        elif body_is_model_dump:
            body_for_solving = _restore_validation_aliases(
                head_dependant.body_params[0].field_info.annotation, request_info.body
            )
        else:
            body_for_solving = request_info.body

        # Remember this: if len(body_params) == 1, then route.body_schema == route.dependant.body_params[0]
        result = await solve_dependencies(
            request=request,
            response=response,
            dependant=head_dependant,
            body=body_for_solving,
            dependency_overrides_provider=head_route.dependency_overrides_provider,
            async_exit_stack=exit_stack,
            embed_body_fields=embed_body_fields,
            background_tasks=background_tasks,
        )
        if result.errors:
            raise CadwynHeadRequestValidationError(result.errors, body=request_info.body, version=current_version)
        return result.values

    def _migrate_response(
        self,
        response_info: ResponseInfo,
        current_version: VersionType,
        head_response_model: object,
        head_route: Union[APIRoute, None],
    ) -> ResponseInfo:
        head_route_id = id(head_route)
        end = self.version_values.index(current_version)
        response_schema_key = _get_schema_migration_key(head_response_model)
        for v in self.versions[:end]:
            for version_change in v.changes:
                migrations_to_apply: list[_BaseAlterResponseInstruction] = []

                if (
                    response_schema_key is not None
                    and response_schema_key in version_change.alter_response_by_schema_instructions
                ):
                    # Membership establishes that this annotation is a registered key.
                    schema = cast("type", response_schema_key)
                    migrations_to_apply.extend(version_change.alter_response_by_schema_instructions[schema])

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
        response_param_name: str,
    ) -> "Callable[[Endpoint[_P, _R]], Callable[..., Coroutine[Any, Any, _R]]]":
        def wrapper(endpoint: "Endpoint[_P, _R]") -> "Callable[..., Coroutine[Any, Any, _R]]":
            @functools.wraps(endpoint)
            async def decorator(*args: Any, **kwargs: Any) -> _R:
                request_param: FastapiRequest = kwargs[request_param_name]
                response_param: FastapiResponse = kwargs[response_param_name]
                background_tasks: Union[BackgroundTasks, None] = (
                    kwargs.get(background_tasks_param_name) if background_tasks_param_name is not None else None
                )
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
                        background_tasks=background_tasks,
                    )

                    response = await self._convert_endpoint_response_to_version(
                        endpoint,
                        head_route,
                        route,
                        method,
                        response_param_name,
                        kwargs,
                        response_param,
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
                return response

            if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _CADWYN_REQUEST_PARAM_NAME, FastapiRequest)
            if response_param_name == _CADWYN_RESPONSE_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _CADWYN_RESPONSE_PARAM_NAME, FastapiResponse)

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
            head_route.response_model,
            head_route,
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
        return response_info.body

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
    ) -> dict[str, Any]:
        request: FastapiRequest = kwargs[request_param_name]
        if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
            kwargs.pop(request_param_name)

        api_version = self.api_version_var.get()
        if api_version is None:
            return kwargs

        body_is_model_dump = False
        # This is a kind of body param you get when you define a single pydantic schema in your route's body
        if (
            len(route.dependant.body_params) == 1
            and head_body_field is not None
            and body_field_alias is not None
            and body_field_alias in kwargs
        ):
            raw_body: Union[BaseModel, None] = kwargs.get(body_field_alias)
            if raw_body is None:  # pragma: no cover # This is likely an impossible case but we would like to be safe
                body = None
            # It means we have a dict or a list instead of a full model.
            # This covers the following use case in the endpoint definition: "payload: dict = Body(None)"
            elif not isinstance(raw_body, BaseModel):
                body = raw_body
            else:
                body = raw_body.model_dump(by_alias=True, exclude_unset=True)
                body_is_model_dump = True
        else:
            # This is for requests without body or with complex body such as form or file
            body = await _get_body(request, route.body_field, exit_stack)

        request_info = RequestInfo(request, body)
        new_kwargs = await self._migrate_request(
            head_body_field,
            head_dependant,
            request,
            response,
            request_info,
            api_version,
            head_route,
            exit_stack=exit_stack,
            embed_body_fields=embed_body_fields,
            background_tasks=background_tasks,
            body_is_model_dump=body_is_model_dump,
        )
        # Because we re-added it into our kwargs when we did solve_dependencies
        if _CADWYN_REQUEST_PARAM_NAME in new_kwargs:
            new_kwargs.pop(_CADWYN_REQUEST_PARAM_NAME)

        return new_kwargs


# We use this instead of `.body()` to automatically guess body type and load the correct body, even if it's a form
async def _get_body(
    request: FastapiRequest, body_field: Union[ModelField, None], exit_stack: AsyncExitStack
):  # pragma: no cover # This is from FastAPI
    is_body_form = body_field and isinstance(body_field.field_info, params.Form)
    try:
        body: Any = None
        if body_field:
            if is_body_form:
                body = await request.form()
                exit_stack.push_async_callback(body.close)
            else:
                body_bytes = await request.body()
                if body_bytes:
                    json_body: Any = PydanticUndefined
                    content_type_value = request.headers.get("content-type")
                    if not content_type_value:
                        json_body = await request.json()
                    else:
                        message = email.message.Message()
                        message["content-type"] = content_type_value
                        if message.get_content_maintype() == "application":
                            subtype = message.get_content_subtype()
                            if subtype == "json" or subtype.endswith("+json"):
                                json_body = await request.json()
                    if json_body != PydanticUndefined:
                        body = json_body
                    else:
                        body = body_bytes
    except json.JSONDecodeError as e:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", e.pos),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": e.msg},
                },
            ],
            body=e.doc,
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail="There was an error parsing the body") from e
    return body


def _get_schema_migration_key(annotation: object) -> object:
    """Return the annotation if it can be looked up in schema migration mappings, otherwise None.

    Some annotations (e.g. `list[Annotated[int, {"description": "item"}]]`) are unhashable,
    so no schema migration can ever be registered for them.
    """
    try:
        hash(annotation)
    except TypeError:
        return None
    return annotation


def _restore_validation_aliases(annotation: object, data: Any) -> Any:
    """Rename the keys of `model_dump(by_alias=True)` data to the keys that the models in `annotation` validate.

    The dump uses serialization aliases, which can differ from what a field validates
    (e.g. `Field(alias="x", validation_alias="v")`), so the head model would silently ignore such values.
    Anything that the restoration plans of the models don't cover is left as is.
    """
    node = _get_restorable_node(annotation)
    if node is None:
        return data
    kind, inner = node
    if kind == "list":
        return [_restore_validation_aliases(inner, item) for item in data] if isinstance(data, list) else data
    if kind == "dict":
        return (
            {key: _restore_validation_aliases(inner, value) for key, value in data.items()}
            if isinstance(data, dict)
            else data
        )
    plan = _get_restore_plan(inner)
    if plan.root_annotation is not None:
        return _restore_validation_aliases(plan.root_annotation, data)
    if (not plan.renames and not plan.nested) or not isinstance(data, dict):
        return data
    restored = dict(data)
    for dumped_key, field_annotation in plan.nested:
        if dumped_key in restored:
            restored[dumped_key] = _restore_validation_aliases(field_annotation, restored[dumped_key])
    for dumped_key, target_key, validation_paths in plan.renames:
        # The head already finds a value for the field if a migration has set one of its validation paths
        if dumped_key in data and all(
            path.search_dict_for_path(data) is PydanticUndefined for path in validation_paths
        ):
            restored[target_key] = restored.pop(dumped_key)
    return restored


def _get_restorable_node(annotation: object) -> Union[tuple[str, Any], None]:
    """Return what the restoration does with the data of `annotation`: walk into a list, a dict, or a model.

    It's the only place that decides it, so planning and restoration can't disagree.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(annotation, UnionType):
        options = [arg for arg in args if arg is not type(None)]
        # With several options, we can't know which one is going to validate the data so we leave it as is
        return _get_restorable_node(options[0]) if len(options) == 1 else None
    if origin is list and len(args) == 1:
        return "list", args[0]
    if origin is dict and len(args) == 2:
        return "dict", args[1]
    if lenient_issubclass(annotation, BaseModel):
        return "model", annotation
    return None


@dataclasses.dataclass(frozen=True)
class _RestorePlan:
    # (dumped_key, target_key, validation_paths) of the fields to rename
    renames: tuple[tuple[str, str, tuple[AliasPath, ...]], ...] = ()
    # (dumped_key, annotation) of the fields to walk into
    nested: tuple[tuple[str, object], ...] = ()
    # The annotation to walk into for root models
    root_annotation: object = None


@functools.cache
def _get_restore_plan(model: type[BaseModel]) -> _RestorePlan:
    renames, traversable = _get_renames_and_traversable_fields(model)
    if issubclass(model, RootModel):
        return _RestorePlan(
            root_annotation=traversable[0][1] if traversable and _leads_to_renames(traversable[0][1], set()) else None
        )
    return _RestorePlan(
        renames=renames,
        nested=tuple((key, annotation) for key, annotation in traversable if _leads_to_renames(annotation, set())),
    )


def _leads_to_renames(annotation: object, seen_models: set[type[BaseModel]]) -> bool:
    """Whether the restoration can reach a model with renames when walking into `annotation`"""
    node = _get_restorable_node(annotation)
    if node is None:
        return False
    kind, inner = node
    if kind != "model":
        return _leads_to_renames(inner, seen_models)
    if inner in seen_models:
        return False
    seen_models.add(inner)
    renames, traversable = _get_renames_and_traversable_fields(inner)
    return bool(renames) or any(_leads_to_renames(annotation, seen_models) for _, annotation in traversable)


@functools.cache
def _get_renames_and_traversable_fields(
    model: type[BaseModel],
) -> tuple[tuple[tuple[str, str, tuple[AliasPath, ...]], ...], tuple[tuple[str, object], ...]]:
    """Return the fields of the model to rename, and the fields whose values we can walk into.

    We only touch a field when the dumped value is unambiguously its valid input. Everything else, such as fields
    that can only be validated through an `AliasPath`, is left as on main.
    """
    model_schema, definitions = _get_model_core_schema(model)
    # A client's extra key can't be told apart from a dumped key, and a "before", "wrap" or "plain" model validator
    # might already handle the dumped keys
    if model_schema is None or model.model_config.get("extra") == "allow":
        return (), ()
    if model_schema["root_model"]:
        root_schema = model_schema["schema"]
        if _dumps_invalid_input(root_schema, definitions):
            return (), ()
        return (), (("root", model.model_fields["root"].annotation),)
    field_schemas = model_schema["schema"]["fields"]
    fields = {
        name: (field.serialization_alias or name, _get_validation_paths(model, name, field))
        for name, field in model.model_fields.items()
    }
    dumped_key_owners: dict[str, set[str]] = defaultdict(set)
    validated_key_owners: dict[Union[str, int], set[str]] = defaultdict(set)
    for name, (dumped_key, paths) in fields.items():
        dumped_key_owners[dumped_key].add(name)
        for path in paths:
            validated_key_owners[path[0]].add(name)
    renames = []
    traversable = []
    for name, (dumped_key, paths) in fields.items():
        # A key that belongs to several fields is ambiguous, e.g. another field might read into it via `AliasPath`
        if (
            _dumps_invalid_input(field_schemas[name]["schema"], definitions)
            or dumped_key_owners[dumped_key] != {name}
            or not validated_key_owners.get(dumped_key, set()) <= {name}
        ):
            continue
        keys = [path[0] for path in paths if len(path) == 1]
        if [dumped_key] in paths:
            traversable.append((dumped_key, model.model_fields[name].annotation))
        elif keys and keys[0] not in dumped_key_owners and validated_key_owners[keys[0]] == {name}:
            target_key = cast("str", keys[0])
            renames.append((dumped_key, target_key, tuple(AliasPath(cast("str", p[0]), *p[1:]) for p in paths)))
            traversable.append((dumped_key, model.model_fields[name].annotation))
    return tuple(renames), tuple(traversable)


_VALIDATORS_OF_RAW_INPUT = ("function-before", "function-wrap", "function-plain")


def _get_model_core_schema(model: type[BaseModel]) -> tuple[Union[dict[str, Any], None], dict[str, Any]]:
    """Return the `model` core schema of the model, or None if a validator of the model gets the raw input"""
    definitions = _collect_core_schema_definitions(model.__pydantic_core_schema__, {})
    schema = _resolve_core_schema(cast("dict[str, Any]", model.__pydantic_core_schema__), definitions)
    while schema["type"] != "model":
        # "wrap" and "plain" model validators wrap the model schema, "after" ones too
        if schema["type"] in _VALIDATORS_OF_RAW_INPUT:
            return None, definitions
        schema = _resolve_core_schema(schema["schema"], definitions)
    # A model serializer can dump anything
    if _is_custom_serialization(schema.get("serialization")):
        return None, definitions
    inner_schema = schema["schema"]
    # "before" model validators (including `root_validator(pre=True)`) wrap the fields schema inside the model schema
    if inner_schema["type"] in _VALIDATORS_OF_RAW_INPUT and not schema["root_model"]:
        return None, definitions
    return schema, definitions


def _resolve_core_schema(schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    while schema["type"] in ("definitions", "definition-ref"):
        schema = schema["schema"] if schema["type"] == "definitions" else definitions[schema["schema_ref"]]
    return schema


def _collect_core_schema_definitions(schema: object, definitions: dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema, dict):
        if schema.get("type") == "definitions":
            definitions.update((definition["ref"], definition) for definition in schema["definitions"])
        for key, value in schema.items():
            if key != "metadata":
                _collect_core_schema_definitions(value, definitions)
    elif isinstance(schema, list):
        for value in schema:
            _collect_core_schema_definitions(value, definitions)
    return definitions


def _dumps_invalid_input(field_schema: dict[str, Any], definitions: dict[str, Any]) -> bool:
    """Whether the dumped value of a field with this core schema might not be the valid input of the field"""
    schema = field_schema
    while schema["type"] in ("default", "function-after"):
        schema = schema["schema"]
    # A field validator that gets the raw input might not expect the dumped value
    return schema["type"] in _VALIDATORS_OF_RAW_INPUT or _has_custom_serialization(field_schema, definitions, set())


def _has_custom_serialization(schema: object, definitions: dict[str, Any], seen_refs: set[str]) -> bool:
    """Whether the core schema dumps values that might not be valid input in Python mode.

    That's the case for custom serializers, `Json`, and models that forbid extra keys but dump computed fields.
    """
    if isinstance(schema, list):
        return any(_has_custom_serialization(value, definitions, seen_refs) for value in schema)
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "definition-ref":
        if schema["schema_ref"] in seen_refs:
            return False
        seen_refs.add(schema["schema_ref"])
        return _has_custom_serialization(definitions[schema["schema_ref"]], definitions, seen_refs)
    if _is_custom_serialization(schema.get("serialization")):
        return True
    if schema.get("type") == "json" or (
        schema.get("type") in ("model", "dataclass")
        and schema.get("config", {}).get("extra_fields_behavior") == "forbid"
        and _has_computed_fields(schema["schema"])
    ):
        return True
    return any(
        _has_custom_serialization(value, definitions, seen_refs) for key, value in schema.items() if key != "metadata"
    )


def _is_custom_serialization(serialization: object) -> bool:
    # Serializers that only run in JSON mode don't affect `model_dump()`
    return isinstance(serialization, dict) and serialization.get("when_used") not in ("json", "json-unless-none")


def _has_computed_fields(schema: object) -> bool:
    if isinstance(schema, dict):
        return bool(schema.get("computed_fields")) or any(_has_computed_fields(value) for value in schema.values())
    return isinstance(schema, list) and any(_has_computed_fields(value) for value in schema)


def _get_validation_paths(model: type[BaseModel], name: str, field: FieldInfo) -> list[list[Union[str, int]]]:
    """Return the paths in the input data that the field validates, in the order Pydantic tries them"""
    alias = field.validation_alias
    paths: list[list[Union[str, int]]] = []
    if not model.model_config.get("validate_by_alias", True) or alias is None:
        paths.append([name])
    elif isinstance(alias, str):
        paths.append([alias])
    elif isinstance(alias, AliasPath):
        paths.append(alias.convert_to_aliases())
    else:
        paths.extend(alias.convert_to_aliases())
    if model.model_config.get("validate_by_name", model.model_config.get("populate_by_name", False)):
        paths.append([name])
    return paths


def _add_keyword_only_parameter(
    func: Callable[..., object],
    param_name: str,
    param_annotation: type,
):
    signature = inspect.signature(func)
    func.__signature__ = signature.replace(  # ty: ignore[unresolved-attribute]
        parameters=[
            *list(signature.parameters.values()),
            inspect.Parameter(param_name, kind=inspect._ParameterKind.KEYWORD_ONLY, annotation=param_annotation),
        ]
    )
