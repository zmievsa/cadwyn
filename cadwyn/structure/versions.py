import dataclasses
import email.message
import functools
import http
import inspect
import itertools
import json
import os
import tempfile
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Coroutine, Hashable, Iterator, Sequence
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Annotated, BinaryIO, ClassVar, TypeAlias, Union, cast, get_args, get_origin

from fastapi import BackgroundTasks, HTTPException, params
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi._compat import ModelField, is_bytes_or_nonable_bytes_annotation, is_bytes_sequence_annotation
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_validation_alias, solve_dependencies
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import AliasChoices, AliasPath, BaseModel, RootModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from starlette._utils import is_async_callable
from starlette.datastructures import FormData, UploadFile
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


def _get_omitted_body_marker() -> Any:
    """Return what FastAPI passes to `solve_dependencies` for a request without a body.

    It is `None` before FastAPI 0.151.0, and `PydanticUndefined` since then, because FastAPI 0.151.0 validates `None`
    as a JSON null body.
    """
    body_param = inspect.signature(solve_dependencies).parameters.get("body")
    if body_param is None or body_param.default is inspect.Parameter.empty:  # pragma: no cover
        raise ImportError(
            "Cadwyn does not support this version of FastAPI: `fastapi.dependencies.utils.solve_dependencies` has "
            "no default for its `body` parameter, which Cadwyn uses to pass requests without a body to FastAPI. "
            "Please, open an issue on Cadwyn's GitHub."
        )
    return body_param.default


_OMITTED_BODY: Any = _get_omitted_body_marker()

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
        body_is_omitted: bool,
        uploads_read_by_fastapi: "Union[_UploadPositions, None]",
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

        is_body_restored = False
        if request_info._form is not None:
            if uploads_read_by_fastapi:
                await _rewind_uploads_read_by_fastapi(request_info._form, uploads_read_by_fastapi)
            body_for_solving = FormData(request_info._form)
        # A body is omitted if the request had none for FastAPI, or if a migration has set it to None
        elif request_info.body is None and (request_info._body_is_assigned or body_is_omitted):
            body_for_solving = _OMITTED_BODY
        elif body_is_model_dump:
            body_for_solving = _rescue_body(head_dependant.body_params[0], request_info.body)
            is_body_restored = body_for_solving is not request_info.body
        else:
            body_for_solving = request_info.body

        # Remember this: if len(body_params) == 1, then route.body_schema == route.dependant.body_params[0]
        solve = functools.partial(
            solve_dependencies,
            request=request,
            response=response,
            dependant=head_dependant,
            dependency_overrides_provider=head_route.dependency_overrides_provider,
            async_exit_stack=exit_stack,
            embed_body_fields=embed_body_fields,
            background_tasks=background_tasks,
        )
        result = await solve(body=body_for_solving)
        # We only restore bodies that would fail as they are. If the head rejects the restored body too, we solve the
        # request again with the body as it is, so that it fails with the same error as before. The second solve reuses
        # the dependencies of the first one, so they run as many times as before.
        if result.errors and is_body_restored:
            result = await solve(body=request_info.body, dependency_cache=result.dependency_cache)
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
    ) -> dict[str, Any]:
        request: FastapiRequest = kwargs[request_param_name]
        if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
            kwargs.pop(request_param_name)

        api_version = self.api_version_var.get()
        if api_version is None:
            return kwargs

        body_is_model_dump = False
        uploads_read_by_fastapi: Union[_UploadPositions, None] = None
        # This is a kind of body param you get when you define a single pydantic schema in your route's body
        if (
            len(route.dependant.body_params) == 1
            and head_body_field is not None
            and body_field_alias is not None
            and body_field_alias in kwargs
            and _kwarg_can_be_the_body(route, body_field_alias, embed_body_fields=embed_body_fields)
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
            if isinstance(body, FormData):
                uploads_read_by_fastapi = _get_uploads_read_by_fastapi(route, head_route, head_dependant, body)

        body_is_omitted = body is None and await _fastapi_got_no_body(
            request, route, embed_body_fields=embed_body_fields
        )
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
            body_is_omitted=body_is_omitted,
            uploads_read_by_fastapi=uploads_read_by_fastapi,
        )
        # Because we re-added it into our kwargs when we did solve_dependencies
        if _CADWYN_REQUEST_PARAM_NAME in new_kwargs:
            new_kwargs.pop(_CADWYN_REQUEST_PARAM_NAME)

        return new_kwargs


async def _fastapi_got_no_body(request: FastapiRequest, route: APIRoute, *, embed_body_fields: bool) -> bool:
    """Return whether FastAPI has solved the versioned route without a body, given that Cadwyn sees its body as None.

    Like FastAPI, we only look at the raw request: FastAPI passes a body if the route has a body field and the request
    has a form or non-empty bytes.
    """
    if route.body_field is None:
        return True
    is_form = isinstance(route.body_field.field_info, params.Form)
    # FastAPI always passes forms. It has already read other bodies, so this returns the cached bytes
    if not is_form and not await request.body():
        return True
    # Embedded body fields get None only if `_get_body` has parsed a body without a content type as JSON null.
    # FastAPI rejects a JSON null for them, so it has got these raw bytes instead, which it ignores like no body
    return embed_body_fields and not is_form


def _kwarg_can_be_the_body(route: APIRoute, body_field_alias: str, *, embed_body_fields: bool) -> bool:
    """Return False if the endpoint kwarg named after the body's alias is surely not the body.

    FastAPI keys the endpoint kwargs by param names, while the body is looked up by its alias. So the kwarg
    is another param when the body is embedded (wrapped into a separate field with the "body" alias),
    or when the alias isn't the name of the body param, unless the kwarg is a dependency that returns the body.
    """
    # FastAPI has already read the uploaded files, so they would come out empty if we read them again
    if route.body_field is not None and isinstance(route.body_field.field_info, params.File):
        return True
    if embed_body_fields:
        return False
    return body_field_alias == route.dependant.body_params[0].name or any(
        dependency.name == body_field_alias for dependency in route.dependant.dependencies
    )


@dataclasses.dataclass(frozen=True)
class _UploadReadByFastapi:
    form_key: str
    is_sequence: bool
    upload: UploadFile  # Keeps `id(upload)` valid
    file: BinaryIO
    position: int


# Maps `id(upload)` to the upload with its form key, the kind of the param that read it, its file,
# and its position right after FastAPI has read it
_UploadPositions: TypeAlias = dict[int, _UploadReadByFastapi]


def _get_uploads_read_by_fastapi(
    route: APIRoute, head_route: APIRoute, head_dependant: Dependant, form: FormData
) -> _UploadPositions:
    """Return the uploaded files that FastAPI has read for the `bytes` file params of the versioned route.

    FastAPI reads these files to the end, so the head route would get them empty if we passed them on as is.
    These params can be declared by the endpoint or by any of its dependencies. We only take the params
    that the head route also has, look the files up the same way FastAPI does, and only consider the spooled
    files that starlette creates.
    """
    uploads: _UploadPositions = {}
    # The endpoint's own params come first and in their order, and the files they read are recorded as before
    own_params = _get_bytes_file_params_of_fields(route.dependant.body_params)
    for alias, is_sequence in own_params:
        for value in _get_form_values(form, alias, is_sequence=is_sequence):
            if isinstance(value, UploadFile) and isinstance(value.file, tempfile.SpooledTemporaryFile):
                uploads[id(value)] = _UploadReadByFastapi(alias, is_sequence, value, value.file, value.file.tell())
    dependency_params = _get_bytes_file_params_of_dependencies(route, head_route, head_dependant) - set(own_params)
    # If a scalar and a sequence param read the same file, the head must pick it as the scalar to get it rewound
    for alias, is_sequence in sorted(dependency_params, key=lambda param: param[1]):
        for upload, position in _iter_uploads_read_to_the_end(form, alias, is_sequence=is_sequence):
            uploads.setdefault(id(upload), _UploadReadByFastapi(alias, is_sequence, upload, upload.file, position))
    return uploads


def _iter_uploads_read_to_the_end(form: FormData, alias: str, *, is_sequence: bool) -> Iterator[tuple[UploadFile, int]]:
    """Yield the open spooled files that FastAPI picks for the param, with their positions, if they are at the end.

    FastAPI reads these files to the end. A file that is elsewhere was moved by a dependency after that,
    e.g. to skip a header, so we leave it as it is. We check the end by seeking, which doesn't read the file.
    """
    for value in _get_form_values(form, alias, is_sequence=is_sequence):
        if (
            isinstance(value, UploadFile)
            and isinstance(value.file, tempfile.SpooledTemporaryFile)
            and not value.file.closed
        ):
            position = value.file.tell()
            end = value.file.seek(0, os.SEEK_END)
            value.file.seek(position)
            if position == end:
                yield value, position


def _get_dependency_overrides(route: APIRoute) -> dict[Any, Any]:
    return getattr(route.dependency_overrides_provider, "dependency_overrides", None) or {}


def _get_bytes_file_params_of_dependencies(
    route: APIRoute, head_route: APIRoute, head_dependant: Dependant
) -> set[tuple[str, bool]]:
    """Return `(alias, is_sequence)` of the `bytes` file params of dependencies whose files the head can get again.

    FastAPI reads the body params of every dependency, including the cached ones, so we walk the whole trees.
    We only take the params that the head's tree also has, and none at all if the versioned route has
    dependencies that the head doesn't have.
    """
    dependencies = list(_iter_dependencies(route.dependant, _get_dependency_overrides(route)))
    bytes_file_params = {
        param
        for _, dependant in dependencies
        if dependant is not None
        for param in _get_bytes_file_params_of_fields(dependant.body_params)
    }
    if not bytes_file_params:
        return bytes_file_params
    head_dependencies = list(_iter_dependencies(head_dependant, _get_dependency_overrides(head_route)))
    bytes_file_params &= {
        param
        for dependant in [head_dependant, *(dependant for _, dependant in head_dependencies)]
        if dependant is not None
        for param in _get_bytes_file_params_of_fields(dependant.body_params)
    }
    # Counted, so that a versioned route that runs a dependency more often than the head also counts as adding it
    head_call_keys = Counter(call_key for call_key, _ in head_dependencies)
    # A dependency that only the versioned route has won't run again for the head, so a rewind would undo what it
    # did with a file. It can reach any file, e.g. through `Request.form()` or a sub-dependency that the head also
    # has, so we leave the files of all dependencies as they are
    if Counter(call_key for call_key, _ in dependencies) - head_call_keys:
        return set()
    return bytes_file_params


def _iter_dependencies(
    dependant: Dependant, dependency_overrides: dict[Any, Any]
) -> Iterator[tuple[Hashable, Union[Dependant, None]]]:
    """Yield a key of every dependency in the tree that FastAPI solves, with the dependency's dependant.

    The keys of the same dependency are equal in the trees of all versions. Like FastAPI's dependency cache keys,
    they consist of the callable that FastAPI calls, all security scopes of the dependency, and its scope. Versioned
    routes use copies of dependencies that compare equal to the originals, and FastAPI already hashes these
    callables. Overrides come from the same dict for every route but can be unhashable, so we key them by identity,
    together with the overridden callable (a key of that dict) because FastAPI keeps dependencies with the same
    override apart.
    FastAPI solves the dependant of an override instead of the overridden dependency, so for an overridden
    dependency we yield no dependant and skip its sub-dependencies.
    """
    for sub_dependant in dependant.dependencies:
        call = sub_dependant.call
        scopes = tuple(sorted({*(sub_dependant.parent_oauth_scopes or ()), *(sub_dependant.own_oauth_scopes or ())}))
        # Same lookup as in FastAPI, which only looks up overrides when there are any
        if dependency_overrides and (override := dependency_overrides.get(call, call)) is not call:
            yield (_OVERRIDE_KEY, call, id(override), scopes, sub_dependant.scope), None
        else:
            yield (call, scopes, sub_dependant.scope), sub_dependant
            yield from _iter_dependencies(sub_dependant, dependency_overrides)


# Overrides stay alive in the overrides dict, so their ids identify them while we compare the trees
_OVERRIDE_KEY = object()


def _get_bytes_file_params_of_fields(fields: Sequence[ModelField]) -> list[tuple[str, bool]]:
    """Return `(alias, is_sequence)` of the `bytes` file params among the fields, in their order."""
    bytes_file_params: list[tuple[str, bool]] = []
    for field in fields:
        if isinstance(field.field_info, params.File):
            annotation = field.field_info.annotation
            if is_bytes_or_nonable_bytes_annotation(annotation):
                bytes_file_params.append((get_validation_alias(field), False))
            elif is_bytes_sequence_annotation(annotation):
                bytes_file_params.append((get_validation_alias(field), True))
    return bytes_file_params


def _get_form_values(form: FormData, key: str, *, is_sequence: bool) -> list[Union[UploadFile, str, None]]:
    """Return the values that FastAPI picks from the form for a `bytes` (or a sequence of `bytes`) file param."""
    return list(form.getlist(key)) if is_sequence else [form.get(key)]


async def _rewind_uploads_read_by_fastapi(
    form: list[tuple[str, Union[UploadFile, str]]], uploads: _UploadPositions
) -> None:
    """Undo FastAPI's reading of the uploaded files that the head will get exactly where FastAPI left them.

    We only rewind a file if the head's param will pick it from the same key again, if its stream belongs to
    this one upload in the form and appears there only once, and if the stream is still at the same position.
    Files that migrations replaced, shadowed, reused, moved under other keys, closed, seeked, swapped for other
    streams, or whose streams they shared with other uploads are passed on exactly as the migrations left them.
    """
    # A single pass over the form, so that forms with many files take linear time
    uploads_per_stream: Counter[int] = Counter()
    last_value_ids: dict[str, int] = {}
    value_ids: defaultdict[str, set[int]] = defaultdict(set)
    for key, value in form:
        if isinstance(value, UploadFile):
            uploads_per_stream[id(value.file)] += 1
        last_value_ids[key] = id(value)
        value_ids[key].add(id(value))
    for read in uploads.values():
        if read.is_sequence:
            is_picked = id(read.upload) in value_ids[read.form_key]
        else:
            # Like FastAPI, the head takes the last value of the key
            is_picked = last_value_ids.get(read.form_key) == id(read.upload)
        if (
            is_picked
            and read.upload.file is read.file
            and uploads_per_stream[id(read.file)] == 1
            and not read.file.closed
            and read.file.tell() == read.position
        ):
            await read.upload.seek(0)


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


# Whether the last restoration renamed a key without which the data surely fails validation
_RESTORATION = threading.local()


def _rescue_body(body_field: ModelField, data: Any) -> Any:
    """Restore the validation aliases of a `model_dump(by_alias=True)` body only if it surely fails validation as is.

    The dump uses serialization aliases, which can differ from what a field validates
    (e.g. `Field(alias="x", validation_alias="v")`), so the model would silently ignore such values.
    That makes the body surely fail when a required field, or a model that forbids extra keys, gets a dumped key
    instead of a validation key. Bodies that the field accepts as they are stay exactly as they are, even if a field
    gets its default instead of its value. Anything that the restoration plans of the models don't cover is left as is.
    """
    annotation = body_field.field_info.annotation
    # Most bodies have no fields without which they fail, so they aren't walked at all
    if not _annotation_reaches_models_with(annotation, _get_failing_renames, all_fields=False):
        return data
    spec = _get_restorer_spec(annotation)
    if spec is None or not _is_body_field_safe(body_field, spec):
        return data
    _RESTORATION.has_failing_renames = False
    restored = _restore_data(spec, data)
    return restored if _RESTORATION.has_failing_renames else data


def _rescue_response(response_field: ModelField, head_response_model: object, content: Any, data: Any) -> Any:
    """Rescue the migrated `_prepare_response_content` of `content` if it surely fails validation due to dumped keys.

    That's the case when a required field, or a model that forbids extra keys, gets a dumped key instead of
    a validation key. We validate the response with its validation aliases restored, and use the result only if it
    passes. FastAPI then validates the result again, which doesn't run any validators of the validated models.
    Otherwise, FastAPI validates the response as it is, as it always did, and responses that pass stay exactly
    as they are, even if a field gets its default instead of its value.
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
    # The response fails as it is anyway, so whatever goes wrong while we try to rescue it, we don't swallow
    # anything new: FastAPI validates the response as it is and raises the original error as before
    try:
        value, errors = response_field.validate(restored, {}, loc=("response",))
    except Exception:  # noqa: BLE001
        return data
    return data if errors else value


def _restore_model_validation_aliases(model: type[BaseModel], data: Any) -> Any:
    """Restore the validation aliases in `model_dump(by_alias=True)` data that `model.model_validate` validates"""
    return _restore_data(("model", model), data)


def _is_dumped_from_models(content: Any, spec: "_RestorerSpec") -> bool:
    """Whether the content holds models of the spec wherever the spec expects a model, and holds at least one.

    Subclasses only count if they dump the fields of the model like the model does. Other values, such as None,
    mean that whatever is in the migrated data didn't come from the dump of a model. We don't look into the models:
    their fields hold their own models.
    """
    stack: list[tuple[Any, _RestorerSpec]] = [(content, spec)]
    has_models = False
    while stack:
        content, (kind, inner) = stack.pop()
        if kind == "model":
            if isinstance(content, BaseModel):
                if not _dumps_like(type(content), inner):
                    return False
                has_models = True
            elif issubclass(inner, RootModel):
                root_spec = _get_restorer_spec(inner.model_fields["root"].annotation)
                if root_spec is None:
                    return False
                stack.append((content, root_spec))
            elif isinstance(content, dict):
                return False
        elif isinstance(content, BaseModel):
            return False
        elif (kind == "list" and isinstance(content, list)) or (kind == "dict" and isinstance(content, dict)):
            items = content if kind == "list" else content.values()
            # Most containers only hold the models themselves, so we check their types in bulk
            item_types = set(map(type, items))
            if inner[0] == "model" and all(_dumps_like(item_type, inner[1]) for item_type in item_types):
                has_models = has_models or bool(item_types)
            else:
                stack.extend(zip(items, itertools.repeat(inner)))
    return has_models


@functools.cache
def _dumps_like(cls: type, model: type[BaseModel]) -> bool:
    """Whether `model_dump()` of `cls` has the keys and values that a dump of `model` has, for the fields of `model`.

    Subclasses can change how fields are dumped, e.g. with serializers or a `model_dump()` override, and add keys that
    the model reads. We only compare by identity and strings, so we never call the `__eq__` of user values such as
    the defaults of fields.
    """
    if cls is model:
        return True
    if (
        not issubclass(cls, model)
        or issubclass(model, RootModel)
        or _get_model_inner_core_schema(cls) is None
        or _get_validation_settings(cls)[2] == "allow"
    ):
        return False
    # The keys that the fields of the model are dumped to or validated from
    model_keys: set[Union[str, int]] = {cf.alias or name for name, cf in model.model_computed_fields.items()}
    for name, field in model.model_fields.items():
        model_keys.add(field.serialization_alias or name)
        model_keys.update(path[0] for path in _get_validation_paths(model, name, field))
    for name, field in cls.model_fields.items():
        model_field = model.model_fields.get(name)
        if model_field is None:
            if (field.serialization_alias or name) in model_keys:
                return False
        elif not _is_field_dumped_like(cls, field, model, model_field, name):
            return False
    for name, computed_field_info in cls.model_computed_fields.items():
        key = computed_field_info.alias or name
        model_computed_field_info = model.model_computed_fields.get(name)
        if key in model_keys and (
            model_computed_field_info is None or (model_computed_field_info.alias or name) != key
        ):
            return False
    return True


def _is_field_dumped_like(
    cls: type[BaseModel], field: FieldInfo, model: type[BaseModel], model_field: FieldInfo, name: str
) -> bool:
    """Whether the field of the subclass `cls` is dumped like the field of `model`, judging by what the dump uses.

    Its type (including the metadata of `Annotated`, such as `PlainSerializer`), its serialization alias, and its
    field serializers. Validators and defaults don't matter because the dumped value has already been validated.
    """
    cls_serializers = _get_field_serializers(cls, name)
    model_serializers = _get_field_serializers(model, name)
    return (
        field.annotation is model_field.annotation
        and field.serialization_alias == model_field.serialization_alias
        and len(field.metadata) == len(model_field.metadata)
        and all(item is model_item for item, model_item in zip(field.metadata, model_field.metadata, strict=True))
        and len(cls_serializers) == len(model_serializers)
        and all(func is model_func for func, model_func in zip(cls_serializers, model_serializers, strict=True))
    )


def _get_field_serializers(model: type[BaseModel], name: str) -> list[Callable[..., Any]]:
    """Return the functions of the field serializers of the field in the order of their names"""
    decorators = model.__pydantic_decorators__.field_serializers
    return [
        decorators[var_name].func
        for var_name in sorted(decorators)
        if name in decorators[var_name].info.fields or "*" in decorators[var_name].info.fields
    ]


_IS_BODY_FIELD_SAFE: dict[int, tuple[ModelField, bool]] = {}


def _is_body_field_safe(body_field: ModelField, spec: "_RestorerSpec") -> bool:
    """Whether the validators of the body parameter (or response field) itself leave the dumped data as valid input.

    They come from the parameter's annotation, such as `Annotated[Model, BeforeValidator(...)]`, and wrap the model
    (and the lists and dicts around it, which `spec` describes) in the core schema that FastAPI validates the body with.
    Its serializers don't matter because we dump the model.
    """
    cached = _IS_BODY_FIELD_SAFE.get(id(body_field))
    if cached is not None and cached[0] is body_field:
        return cached[1]
    try:
        schema = _register_core_schema_definitions(cast("dict[str, Any]", body_field._type_adapter.core_schema))
        while True:
            # `OnErrorOmit` and other defaults that swallow errors make invalid data pass, so we stop at them
            while schema["type"] in ("definition-ref", "nullable", "default", "function-after") and (
                schema.get("on_error", "raise") == "raise"
            ):
                schema = (
                    _CORE_SCHEMA_DEFINITIONS[schema["schema_ref"]]
                    if schema["type"] == "definition-ref"
                    else schema["schema"]
                )
            kind, inner = spec
            # The model's own validators and serializers are covered by its restoration plan,
            # and the keys of dicts are never renamed
            if kind == "model" or schema["type"] != kind:
                is_safe = kind == schema["type"] == "model"
                break
            schema = schema["items_schema" if kind == "list" else "values_schema"]
            spec = inner
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas
        is_safe = False
    _IS_BODY_FIELD_SAFE[id(body_field)] = (body_field, is_safe)
    return is_safe


# What the restoration does with a value: `("list", item_spec)`, `("dict", value_spec)`, or `("model", model)`
_RestorerSpec: TypeAlias = tuple[str, Any]
# Marks the root of a root model, which replaces the model's data instead of a key in it
_ROOT_KEY = object()


def _get_restorer_spec(annotation: object) -> Union[_RestorerSpec, None]:
    """Return what to restore in the data of `annotation`, or None if there's nothing to restore"""
    node = _get_restorable_node(annotation)
    if node is None:
        return None
    kind, inner = node
    if kind == "model":
        return node
    item_spec = _get_restorer_spec(inner)
    return None if item_spec is None else (kind, item_spec)


def _needs_restoring(spec: _RestorerSpec, data: Any, *, look_ahead: bool = True) -> bool:
    kind, inner = spec
    if kind == "list":
        return isinstance(data, list) and bool(data)
    if kind == "dict":
        return isinstance(data, dict) and bool(data)
    plan = _get_restore_plan(inner)
    if plan.root_spec is not None:
        return _needs_restoring(plan.root_spec, data, look_ahead=look_ahead)
    # Most bulk items have none of the keys, so they only cost this check
    return (
        isinstance(data, dict)
        and not data.keys().isdisjoint(plan.keys)
        and _model_needs_restoring(plan, data, look_ahead=look_ahead)
    )


def _model_needs_restoring(plan: "_RestorePlan", data: dict[str, Any], *, look_ahead: bool) -> bool:
    """Whether a rename applies to the data of the model, or one of its nested values might need restoring.

    We look at most one level deep, so that checking deep bodies stays cheap and never recurses deeply.
    """
    # Plain loops instead of `any()` because this runs for every bulk item that has one of the plan's keys
    for rename in plan.renames:
        if _does_rename_apply(rename, data):
            return True
    for key, child_spec in plan.nested:
        if key in data and (not look_ahead or _needs_restoring(child_spec, data[key], look_ahead=False)):
            return True
    return False


def _does_rename_apply(rename: "_Rename", data: dict[str, Any]) -> bool:
    dumped_key, _, validation_paths, validation_keys = rename
    if dumped_key not in data:
        return False
    # The head already finds a value for the field if a migration has set one of its validation paths
    if validation_keys is not None:
        return data.keys().isdisjoint(validation_keys)
    return all(path.search_dict_for_path(data) is PydanticUndefined for path in validation_paths)


def _iter_children_to_restore(spec: _RestorerSpec, data: Any) -> Iterator[tuple[Any, _RestorerSpec, Any]]:
    kind, inner = spec
    if kind == "model":
        plan = _get_restore_plan(inner)
        if plan.root_spec is not None:
            return iter([(_ROOT_KEY, plan.root_spec, data)])
        return (
            (key, child_spec, data[key])
            for key, child_spec in plan.nested
            if key in data and _needs_restoring(child_spec, data[key])
        )
    items = enumerate(data) if kind == "list" else data.items()
    if inner[0] == "model" and (item_plan := _get_restore_plan(inner[1])).root_spec is None:
        # Most bulk items have none of the keys, so they only cost this check
        keys = item_plan.keys
        return (
            (key, inner, item)
            for key, item in items
            if isinstance(item, dict)
            and not item.keys().isdisjoint(keys)
            and _model_needs_restoring(item_plan, item, look_ahead=True)
        )
    return ((key, inner, item) for key, item in items if _needs_restoring(inner, item))


def _restore_data(spec: _RestorerSpec, data: Any) -> Any:
    """Restore the data without recursion, so that bodies as deep as pydantic accepts never hit the recursion limit.

    We only copy the containers in which something changes.
    """
    if not _needs_restoring(spec, data):
        return data
    if _get_spec_height(spec) is not None:
        return _restore_shallow_data(spec, data, is_validated=True)
    # Each frame is [spec, data, children to restore, restored copy of data or None, key of the child being restored,
    # whether the head validates the data of the frame]. It doesn't validate what's under a dumped key.
    stack: list[list[Any]] = [[spec, data, _iter_children_to_restore(spec, data), None, None, True]]
    while True:
        frame = stack[-1]
        child = next(frame[2], None)
        if child is not None:
            key, child_spec, child_data = child
            is_child_validated = frame[5] and (
                frame[0][0] != "model" or _is_nested_key_validated(_get_restore_plan(frame[0][1]), key, frame[1])
            )
            if _get_spec_height(child_spec) is None:
                frame[4] = key
                stack.append(
                    [
                        child_spec,
                        child_data,
                        _iter_children_to_restore(child_spec, child_data),
                        None,
                        None,
                        is_child_validated,
                    ]
                )
            else:
                restored_child = _restore_shallow_data(child_spec, child_data, is_validated=is_child_validated)
                _set_restored_child(frame, key, child_data, restored_child)
            continue
        stack.pop()
        restored_data = _finish_restoring(frame)
        if not stack:
            return restored_data
        _set_restored_child(stack[-1], stack[-1][4], frame[1], restored_data)


def _set_restored_child(parent: list[Any], key: Any, child_data: Any, restored_child: Any) -> None:
    if restored_child is child_data:
        return
    if key is _ROOT_KEY:
        parent[3] = restored_child
        return
    if parent[3] is None:
        parent[3] = list(parent[1]) if isinstance(parent[1], list) else dict(parent[1])
    parent[3][key] = restored_child


# The greatest number of nested models, lists and dicts that we restore with recursion.
# Each of them costs a frame or two, so restoring with recursion stays far from the recursion limit.
_MAX_SHALLOW_HEIGHT = 8
# The number of nested models, lists and dicts in the data of each model, or None if it can be deeper than the maximum
_SHALLOW_HEIGHTS: dict[type[BaseModel], Union[int, None]] = {}


def _get_spec_height(spec: _RestorerSpec) -> Union[int, None]:
    """Return how many models, lists and dicts restoring the data of the spec walks into at most.

    It's None if it's more than we restore with recursion, e.g. for models that refer to themselves.
    """
    containers, model = _split_spec(spec)
    height = _get_model_height(model)
    return None if height is None or containers + height > _MAX_SHALLOW_HEIGHT else containers + height


def _split_spec(spec: _RestorerSpec) -> tuple[int, type[BaseModel]]:
    """Return the number of lists and dicts around the model of the spec, and the model"""
    containers = 0
    while spec[0] != "model":
        containers += 1
        spec = spec[1]
    return containers, spec[1]


def _get_model_height(root: type[BaseModel]) -> Union[int, None]:
    """Return the height of the data of the model, computed without recursion so that it never hits the recursion limit.

    We only cache final heights, so other threads never see unfinished ones.
    """
    if root in _SHALLOW_HEIGHTS:
        return _SHALLOW_HEIGHTS[root]
    # Each frame is [model, the (containers, model) of its nested fields, its height so far, containers of the child]
    stack: list[list[Any]] = [[root, _iter_nested_models(root), 1, 0]]
    models_on_stack = {root}
    child_height: Union[int, None] = 0
    while True:
        frame = stack[-1]
        child = next(frame[1], None)
        if child is not None:
            containers, model = child
            if model in _SHALLOW_HEIGHTS:
                child_height = _SHALLOW_HEIGHTS[model]
            # A model that is being computed refers to itself through the frame's model
            elif model in models_on_stack:
                child_height = None
            else:
                frame[3] = containers
                stack.append([model, _iter_nested_models(model), 1, 0])
                models_on_stack.add(model)
                continue
        else:
            stack.pop()
            models_on_stack.discard(frame[0])
            child_height = frame[2]
            _SHALLOW_HEIGHTS[frame[0]] = child_height
            if not stack:
                return child_height
            frame = stack[-1]
            containers = frame[3]
        if child_height is None or containers + child_height + 1 > _MAX_SHALLOW_HEIGHT:
            # The model is as deep as its child, so we finish it right away
            frame[1] = iter(())
            frame[2] = None
        else:
            frame[2] = max(frame[2], containers + child_height + 1)


def _iter_nested_models(model: type[BaseModel]) -> Iterator[tuple[int, type[BaseModel]]]:
    plan = _get_restore_plan(model)
    # Root models are always walked
    if plan.root_spec is not None:
        return iter([(0, model)])
    return (_split_spec(child_spec) for _, child_spec in plan.nested)


def _restore_shallow_data(spec: _RestorerSpec, data: Any, *, is_validated: bool) -> Any:
    """Restore the data of a spec that has a height with recursion, which avoids the frames of the walk.

    The result is the same as the walk's: the walk only skips the data that wouldn't change.
    """
    kind, inner = spec
    if kind == "model":
        plan = _get_restore_plan(inner)
        if not isinstance(data, dict) or data.keys().isdisjoint(plan.keys):
            return data
        return _restore_shallow_model_data(plan, data, is_validated=is_validated)
    if kind == "list" and isinstance(data, list):
        items = enumerate(data)
    elif kind == "dict" and isinstance(data, dict):
        items = data.items()
    else:
        return data
    restored = None
    plan = _get_restore_plan(inner[1]) if inner[0] == "model" else None
    for key, item in items:
        if plan is None:
            restored_item = _restore_shallow_data(inner, item, is_validated=is_validated)
        elif not isinstance(item, dict):
            continue
        elif not plan.nested:
            # Most bodies are lists of models without nested models to restore, so their items only cost the renames
            restored_item = _rename_keys(plan, item, is_validated=is_validated)
        # Most bulk items have none of the keys, so they only cost this check
        elif item.keys().isdisjoint(plan.keys):
            continue
        else:
            restored_item = _restore_shallow_model_data(plan, item, is_validated=is_validated)
        if restored_item is not item:
            if restored is None:
                restored = list(data) if kind == "list" else dict(data)
            restored[key] = restored_item
    return data if restored is None else restored


def _restore_shallow_model_data(plan: "_RestorePlan", data: dict[str, Any], *, is_validated: bool) -> Any:
    restored = None
    for key, child_spec in plan.nested:
        if key in data:
            child = data[key]
            # The head doesn't validate what's under a dumped key, or under a key that another key of its field shadows
            restored_child = _restore_shallow_data(
                child_spec, child, is_validated=is_validated and _is_nested_key_validated(plan, key, data)
            )
            if restored_child is not child:
                if restored is None:
                    restored = dict(data)
                restored[key] = restored_child
    return _rename_keys(plan, data, restored, is_validated=is_validated)


def _finish_restoring(frame: list[Any]) -> Any:
    (kind, inner), data, _, restored, _, is_validated = frame
    if kind == "model" and (plan := _get_restore_plan(inner)).root_spec is None:
        return _rename_keys(plan, data, restored, is_validated=is_validated)
    return data if restored is None else restored


def _rename_keys(plan: "_RestorePlan", data: dict[str, Any], restored: Any = None, *, is_validated: bool) -> Any:
    """Apply the renames of the plan to the data of a model, or to its restored copy if there is one.

    Only the renames in data that the head validates can make it surely fail.
    """
    for rename in plan.renames:
        # This runs for every bulk item, so it inlines the common case of `_does_rename_apply`
        dumped_key, target_key, _, validation_keys = rename
        if dumped_key in data and (
            data.keys().isdisjoint(validation_keys) if validation_keys is not None else _does_rename_apply(rename, data)
        ):
            if restored is None:
                restored = dict(data)
            restored[target_key] = restored.pop(dumped_key)
            if is_validated and dumped_key in plan.failing_keys:
                _RESTORATION.has_failing_renames = True
    return data if restored is None else restored


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


# Marks computed fields among the owners of dumped keys, apart from the regular fields
_COMPUTED_FIELD = object()
# (dumped_key, target_key, validation_paths, validation_keys): the keys are None unless all paths are plain keys
_Rename: TypeAlias = tuple[str, str, tuple[AliasPath, ...], Union[frozenset[str], None]]


@dataclasses.dataclass(frozen=True)
class _RestorePlan:
    # The fields to rename
    renames: tuple["_Rename", ...] = ()
    # (dumped_key, spec) of the fields to walk into
    nested: tuple[tuple[str, _RestorerSpec], ...] = ()
    # What to restore in the root of root models
    root_spec: Union[_RestorerSpec, None] = None
    # The dumped keys of `renames` and `nested`
    keys: frozenset[str] = frozenset()
    # The dumped keys of `renames` without which the data of the model surely fails validation:
    # the keys of required fields, or all of them if the model forbids extra keys
    failing_keys: frozenset[str] = frozenset()
    # The dumped keys of `renames`
    renamed_keys: frozenset[str] = frozenset()
    # The other keys that the fields of `nested` validate, by their dumped keys. The model ignores the dumped key of
    # such a field if the data has one of them, e.g. a key of `AliasChoices` that comes before it.
    other_validation_keys: dict[str, frozenset[Union[str, int]]] = dataclasses.field(
        default_factory=dict, compare=False
    )


@functools.cache
def _get_restore_plan(model: type[BaseModel]) -> _RestorePlan:
    # Most apps have no fields to rename, so they don't need to pay for reading core schemas at all
    if not _reaches_models_with(model, _get_rename_candidates, all_fields=True):
        return _RestorePlan()
    renames, traversable = _get_renames_and_traversable_fields(model)
    nested = tuple(
        (key, spec)
        for key, annotation in traversable
        if (spec := _get_restorer_spec(annotation)) is not None
        and _annotation_reaches_models_with(annotation, _get_renames, all_fields=False)
    )
    if issubclass(model, RootModel):
        return _RestorePlan(root_spec=nested[0][1] if nested else None)
    keys = frozenset(key for key, *_ in (*renames, *nested))
    return _RestorePlan(
        renames=renames,
        nested=nested,
        keys=keys,
        failing_keys=_get_failing_keys(model, renames),
        renamed_keys=frozenset(key for key, *_ in renames),
        other_validation_keys=_get_other_validation_keys(model, frozenset(key for key, _ in nested)),
    )


def _get_other_validation_keys(
    model: type[BaseModel], nested_keys: frozenset[str]
) -> dict[str, frozenset[Union[str, int]]]:
    other_validation_keys: dict[str, frozenset[Union[str, int]]] = {}
    for name, field in model.model_fields.items():
        dumped_key = field.serialization_alias or name
        if dumped_key in nested_keys:
            keys = frozenset(path[0] for path in _get_validation_paths(model, name, field) if path[0] != dumped_key)
            if keys:
                other_validation_keys[dumped_key] = keys
    return other_validation_keys


def _is_nested_key_validated(plan: _RestorePlan, key: object, data: dict[str, Any]) -> bool:
    """Whether the model of the plan validates what's under the key of its data, the dumped key of one of its fields.

    It doesn't if it's the key of a field that we rename, or if the data has another key of the field, which might
    come first.
    """
    if key in plan.renamed_keys:
        return False
    other_keys = plan.other_validation_keys.get(cast("str", key))
    return other_keys is None or data.keys().isdisjoint(other_keys)


def _get_failing_keys(model: type[BaseModel], renames: tuple["_Rename", ...]) -> frozenset[str]:
    if _get_validation_settings(model)[2] == "forbid":
        return frozenset(dumped_key for dumped_key, *_ in renames)
    required_dumped_keys = {
        field.serialization_alias or name for name, field in model.model_fields.items() if field.is_required()
    }
    return frozenset(dumped_key for dumped_key, *_ in renames if dumped_key in required_dumped_keys)


def _get_failing_renames(model: type[BaseModel]) -> tuple[object, ...]:
    return tuple(_get_restore_plan(model).failing_keys)


def _annotation_reaches_models_with(
    annotation: object, get_items: Callable[[type[BaseModel]], tuple[object, ...]], *, all_fields: bool
) -> bool:
    """Whether walking into the data of `annotation` reaches a model for which `get_items` returns anything"""
    return any(
        _reaches_models_with(model, get_items, all_fields=all_fields) for model in _get_restorable_models(annotation)
    )


_RESTORABLE_MODELS: dict[object, tuple[type[BaseModel], ...]] = {}


def _get_restorable_models(annotation: object) -> tuple[type[BaseModel], ...]:
    # Unhashable annotations (e.g. with dicts in `Annotated`) are rare, so we don't cache them
    cache_key = _get_schema_migration_key(annotation)
    if cache_key is None:
        return tuple(_iter_restorable_models(annotation))
    if cache_key not in _RESTORABLE_MODELS:
        _RESTORABLE_MODELS[cache_key] = tuple(_iter_restorable_models(annotation))
    return _RESTORABLE_MODELS[cache_key]


def _iter_restorable_models(annotation: object) -> Iterator[type[BaseModel]]:
    """Yield the models that the restoration walks into when walking into the data of `annotation`"""
    node = _get_restorable_node(annotation)
    while node is not None and node[0] != "model":
        node = _get_restorable_node(node[1])
    if node is not None:
        yield node[1]


def _get_renames(model: type[BaseModel]) -> tuple[object, ...]:
    # Models without rename candidates don't need their core schemas read
    return _get_renames_and_traversable_fields(model)[0] if _get_rename_candidates(model) else ()


_REACHES_MODELS_WITH: dict[tuple[Callable[..., object], bool, type[BaseModel]], bool] = {}


def _reaches_models_with(
    root: type[BaseModel], get_items: Callable[[type[BaseModel]], tuple[object, ...]], *, all_fields: bool
) -> bool:
    """Whether walking into the data of `root` reaches a model for which `get_items` returns anything.

    We walk into all the fields of the models if `all_fields`, and into their traversable fields otherwise.
    Each model is explored once: if nothing is found, nothing is reachable from any of the explored models either.
    """
    cache_key = (get_items, all_fields, root)
    if cache_key in _REACHES_MODELS_WITH:
        return _REACHES_MODELS_WITH[cache_key]
    explored: set[type[BaseModel]] = set()
    parents: dict[type[BaseModel], type[BaseModel]] = {}
    to_explore = [root]
    while to_explore:
        model = to_explore.pop()
        if model in explored or _REACHES_MODELS_WITH.get((get_items, all_fields, model)) is False:
            continue
        if _REACHES_MODELS_WITH.get((get_items, all_fields, model)) or get_items(model):
            # Everything on the way from the root reaches it too
            while True:
                _REACHES_MODELS_WITH[get_items, all_fields, model] = True
                if model is root:
                    return True
                model = parents[model]
        explored.add(model)
        if all_fields:
            annotations = [field.annotation for field in model.model_fields.values()]
        elif _reaches_models_with(model, _get_rename_candidates, all_fields=True):
            annotations = [annotation for _, annotation in _get_renames_and_traversable_fields(model)[1]]
        else:
            # Nothing below this model has rename candidates, so there's no need to read its core schema
            annotations = []
        for nested in (nested for annotation in annotations for nested in _get_restorable_models(annotation)):
            parents.setdefault(nested, model)
            to_explore.append(nested)
    for model in explored:
        _REACHES_MODELS_WITH[get_items, all_fields, model] = False
    return False


@functools.cache
def _get_rename_candidates(model: type[BaseModel]) -> tuple[str, ...]:
    """Return the fields whose dumped key the model doesn't validate, judging by their field info only"""
    return tuple(
        name
        for name, field in model.model_fields.items()
        for paths in [_get_validation_paths(model, name, field)]
        if [field.serialization_alias or name] not in paths and any(len(path) == 1 for path in paths)
    )


@functools.cache
def _get_renames_and_traversable_fields(
    model: type[BaseModel],
) -> tuple[tuple[_Rename, ...], tuple[tuple[str, object], ...]]:
    """Return the fields of the model to rename, and the fields whose values we can walk into.

    We only touch a field when the dumped value is unambiguously its valid input. Everything else, such as fields
    that can only be validated through an `AliasPath`, is left as on main.
    """
    inner_schema = _get_model_inner_core_schema(model)
    # A client's extra key can't be told apart from a dumped key
    if inner_schema is None or model.model_config.get("extra") == "allow":
        return (), ()
    if issubclass(model, RootModel):
        if not _is_field_schema_safe({"root": inner_schema}, "root"):
            return (), ()
        return (), (("root", model.model_fields["root"].annotation),)
    field_schemas = inner_schema["fields"]
    # The keys of every field decide which renames are unambiguous, so we only trust the field infos if the compiled
    # schema validates and dumps all fields by the same aliases. `__get_pydantic_core_schema__` can change them.
    if set(field_schemas) != set(model.model_fields) or not all(
        _do_compiled_aliases_match(field, field_schemas[name]) for name, field in model.model_fields.items()
    ):
        return (), ()
    fields = {
        name: (field.serialization_alias or name, _get_validation_paths(model, name, field))
        for name, field in model.model_fields.items()
    }
    dumped_key_owners: dict[str, set[object]] = defaultdict(set)
    validated_key_owners: dict[Union[str, int], set[str]] = defaultdict(set)
    for name, (dumped_key, paths) in fields.items():
        dumped_key_owners[dumped_key].add(name)
        for path in paths:
            validated_key_owners[path[0]].add(name)
    # Computed fields are dumped too, so their keys are ambiguous as well
    for name, computed_field_info in model.model_computed_fields.items():
        dumped_key_owners[computed_field_info.alias or name].add((_COMPUTED_FIELD, name))
    renames = []
    traversable = []
    for name, (dumped_key, paths) in fields.items():
        annotation = model.model_fields[name].annotation
        keys = [path[0] for path in paths if len(path) == 1]
        is_validated = [dumped_key] in paths
        # A key that belongs to several fields is ambiguous, e.g. another field might read into it via `AliasPath`
        if (
            dumped_key_owners[dumped_key] != {name}
            or not validated_key_owners.get(dumped_key, set()) <= {name}
            # Fields that neither need a rename nor lead to one don't need their core schemas read
            or (
                is_validated
                and not _annotation_reaches_models_with(annotation, _get_rename_candidates, all_fields=True)
            )
            or not _is_field_schema_safe(field_schemas, name)
        ):
            continue
        if is_validated:
            traversable.append((dumped_key, annotation))
        elif keys and keys[0] not in dumped_key_owners and validated_key_owners[keys[0]] == {name}:
            target_key = cast("str", keys[0])
            validation_keys = frozenset(cast("str", p[0]) for p in paths) if all(len(p) == 1 for p in paths) else None
            validation_paths = tuple(AliasPath(cast("str", p[0]), *p[1:]) for p in paths)
            renames.append((dumped_key, target_key, validation_paths, validation_keys))
            traversable.append((dumped_key, annotation))
    return tuple(renames), tuple(traversable)


# Reading a core schema of an unexpected shape fails with one of these. We leave such models and fields as on main.
_CORE_SCHEMA_ERRORS = (LookupError, TypeError, AttributeError)
# The core schema types whose dumped values are their valid input, and their keys that hold child schemas as
# `(key, holds_several_schemas, is_optional)`. Any other type, such as "json" or the validators that get the raw input
# ("function-before", "function-wrap", "function-plain"), is unsafe.
# Data-valued keys such as defaults, literal values, metadata, or serializers are never walked.
_CHILD_SCHEMA_KEYS: dict[str, tuple[tuple[str, bool, bool], ...]] = {
    **dict.fromkeys(
        (
            *("any", "none", "bool", "int", "float", "decimal", "complex", "str", "bytes", "literal", "enum"),
            *("date", "time", "datetime", "timedelta", "uuid", "url", "multi-host-url", "missing-sentinel"),
            *("is-instance", "is-subclass", "callable"),
        ),
        (),
    ),
    **dict.fromkeys(("list", "set", "frozenset", "generator"), (("items_schema", False, True),)),
    **dict.fromkeys(
        (
            *("nullable", "default", "function-after", "custom-error", "model", "dataclass"),
            *("model-field", "dataclass-field", "typed-dict-field"),
        ),
        (("schema", False, False),),
    ),
    "tuple": (("items_schema", True, False),),
    "dict": (("keys_schema", False, True), ("values_schema", False, True)),
    "lax-or-strict": (("lax_schema", False, False), ("strict_schema", False, False)),
    "json-or-python": (("json_schema", False, False), ("python_schema", False, False)),
    "chain": (("steps", True, False),),
    "union": (("choices", True, False),),
    "tagged-union": (("choices", True, False),),
    "model-fields": (("fields", True, False), ("extras_schema", False, True)),
    "typed-dict": (("fields", True, False), ("extras_schema", False, True)),
    "dataclass-args": (("fields", True, False),),
}
# Parts of core schemas that are summarized separately because many schemas share them: definitions by their ref
# and models by their class. This keeps the reading of core schemas linear in their size.
_CoreSchemaUnit: TypeAlias = tuple[str, object]
_CORE_SCHEMA_DEFINITIONS: dict[str, dict[str, Any]] = {}
_CORE_SCHEMA_MODELS: dict[object, dict[str, Any]] = {}
# Units are kept in the order the schema refers to them, so that the exploration order doesn't depend on hashing
_CORE_SCHEMA_UNIT_SUMMARIES: dict[_CoreSchemaUnit, tuple[bool, tuple[_CoreSchemaUnit, ...]]] = {}
_IS_CORE_SCHEMA_UNIT_SAFE: dict[_CoreSchemaUnit, bool] = {}


def _register_core_schema_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """Remember the definitions of a "definitions" core schema and return the schema that refers to them"""
    if schema["type"] != "definitions":
        return schema
    for definition in schema["definitions"]:
        _CORE_SCHEMA_DEFINITIONS.setdefault(definition["ref"], definition)
    return schema["schema"]


def _do_compiled_aliases_match(field: FieldInfo, field_schema: object) -> bool:
    """Whether the compiled schema validates and dumps the field by the aliases of its field info.

    A custom `__get_pydantic_core_schema__` can change them, and then we don't know which keys the head reads.
    """
    return (
        isinstance(field_schema, dict)
        and _normalize_validation_alias(field_schema.get("validation_alias"))
        == _normalize_validation_alias(field.validation_alias)
        and field_schema.get("serialization_alias") == field.serialization_alias
    )


def _normalize_validation_alias(alias: object) -> object:
    if isinstance(alias, str):
        return [[alias]]
    if isinstance(alias, (AliasPath, AliasChoices)):
        alias = alias.convert_to_aliases()
    # Compiled `AliasChoices` are lists of paths, while a compiled `AliasPath` is a single path
    if isinstance(alias, list) and alias and not all(isinstance(path, list) for path in alias):
        alias = [alias]
    return alias


def _get_model_inner_core_schema(model: type[BaseModel]) -> Union[dict[str, Any], None]:
    """Return the fields core schema of the model (or the root core schema of a root model).

    Return None instead if a validator of the model gets the raw input, if the model has a model serializer,
    or if its core schema isn't shaped like the one of a plain model.
    """
    try:
        schema = _register_core_schema_definitions(cast("dict[str, Any]", model.__pydantic_core_schema__))
        if schema["type"] == "definition-ref":
            schema = _CORE_SCHEMA_DEFINITIONS[schema["schema_ref"]]
        # "after" model validators wrap the model schema and only get the validated model
        while schema["type"] == "function-after":
            schema = schema["schema"]
        # A custom `__init__` might expect the input that the dump doesn't have
        if (
            schema["type"] != "model"
            or schema["cls"] is not model
            or schema.get("custom_init")
            or _is_custom_serialization(schema.get("serialization"))
            or _overrides_dumping(model)
            or not _do_compiled_settings_match(model, schema.get("config"))
        ):
            return None
        inner_schema = schema["schema"] if schema["root_model"] else _get_fields_core_schema(schema)
        # "wrap" and "plain" model validators wrap the model schema, while "before" ones wrap its fields schema
        if not schema["root_model"] and inner_schema["type"] != "model-fields":
            return None
    except _CORE_SCHEMA_ERRORS:
        return None
    return inner_schema


def _get_fields_core_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the fields core schema of a model or dataclass core schema"""
    fields_schema = schema["schema"]
    # "after" validators (including `root_validator(skip_on_failure=True)`) wrap the fields schema
    while fields_schema["type"] == "function-after":
        fields_schema = fields_schema["schema"]
    return fields_schema


def _overrides_dumping(cls: object) -> bool:
    """Whether a class overrides how pydantic dumps it, which its core schema doesn't describe"""
    return any(
        name in vars(base)
        for base in getattr(cls, "__mro__", ())
        if base not in (BaseModel, RootModel, object)
        for name in ("model_dump", "model_dump_json")
    )


def _is_field_schema_safe(schemas: dict[str, Any], key: str) -> bool:
    """Whether the dumped value of the field with the core schema at `schemas[key]` is its valid input"""
    units: dict[_CoreSchemaUnit, None] = {}
    try:
        if _has_unsafe_core_schema(schemas[key], units):
            return False
    except _CORE_SCHEMA_ERRORS:
        return False
    return all(_is_core_schema_unit_safe(unit) for unit in units)


def _is_core_schema_unit_safe(root: _CoreSchemaUnit) -> bool:
    """Whether nothing reachable from the core schema unit makes its dumped values invalid input.

    Each unit is explored once: if nothing unsafe is found, nothing is reachable from any of the explored units either.
    """
    if root in _IS_CORE_SCHEMA_UNIT_SAFE:
        return _IS_CORE_SCHEMA_UNIT_SAFE[root]
    explored: set[_CoreSchemaUnit] = set()
    parents: dict[_CoreSchemaUnit, _CoreSchemaUnit] = {}
    to_explore = [root]
    while to_explore:
        unit = to_explore.pop()
        is_safe = _IS_CORE_SCHEMA_UNIT_SAFE.get(unit)
        if unit in explored or is_safe:
            continue
        is_unsafe, units = (True, ()) if is_safe is False else _summarize_core_schema_unit(unit)
        if is_unsafe:
            # Everything on the way from the root reaches it too
            while True:
                _IS_CORE_SCHEMA_UNIT_SAFE[unit] = False
                if unit == root:
                    return False
                unit = parents[unit]
        explored.add(unit)
        for child in units:
            parents.setdefault(child, unit)
            to_explore.append(child)
    for unit in explored:
        _IS_CORE_SCHEMA_UNIT_SAFE[unit] = True
    return True


def _summarize_core_schema_unit(unit: _CoreSchemaUnit) -> tuple[bool, tuple[_CoreSchemaUnit, ...]]:
    if unit not in _CORE_SCHEMA_UNIT_SUMMARIES:
        kind, key = unit
        body = (_CORE_SCHEMA_DEFINITIONS if kind == "ref" else _CORE_SCHEMA_MODELS).get(key)
        units: dict[_CoreSchemaUnit, None] = {}
        try:
            # A definition that we haven't seen is unsafe
            is_unsafe = body is None or _has_unsafe_core_schema(body, units, is_unit_body=True)
        except _CORE_SCHEMA_ERRORS:
            is_unsafe = True
        _CORE_SCHEMA_UNIT_SUMMARIES[unit] = (is_unsafe, tuple(units))
    return _CORE_SCHEMA_UNIT_SUMMARIES[unit]


def _has_unsafe_core_schema(
    schema: dict[str, Any], units: dict[_CoreSchemaUnit, None], *, is_unit_body: bool = False
) -> bool:
    """Whether the core schema itself makes its dumped values invalid input. Collect the units it refers to.

    That's the case for custom serializers, `Json`, `OnErrorOmit`, models that forbid extra keys but dump computed
    fields, models with a custom `__init__`, validators that get the raw input, and core schema types we don't know.
    """
    # References and models can carry serializers too, e.g. `Annotated[Model, PlainSerializer(...)]`,
    # so we check them before we summarize the schemas that they refer to
    if _is_custom_serialization(schema.get("serialization")):
        return True
    # `OnErrorOmit` and other defaults that swallow errors make invalid data pass, so we can't tell what the dumped
    # keys break
    if schema.get("on_error", "raise") != "raise":
        return True
    schema_type = schema["type"]
    if schema_type == "definition-ref":
        units["ref", schema["schema_ref"]] = None
        return False
    if schema_type == "model" and not is_unit_body:
        _CORE_SCHEMA_MODELS.setdefault(schema["cls"], schema)
        units["model", schema["cls"]] = None
        return False
    if (
        schema_type not in _CHILD_SCHEMA_KEYS
        or schema.get("custom_init")
        or (schema_type == "model" and _overrides_dumping(schema["cls"]))
        or (
            schema_type in ("model", "dataclass")
            and schema.get("config", {}).get("extra_fields_behavior") == "forbid"
            and _get_fields_core_schema(schema).get("computed_fields")
        )
    ):
        return True
    return any(_has_unsafe_core_schema(child, units) for child in _get_child_core_schemas(schema))


def _get_child_core_schemas(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for key, holds_several, is_optional in _CHILD_SCHEMA_KEYS[schema["type"]]:
        if is_optional and key not in schema:
            continue
        child = schema[key]
        if holds_several:
            for item in child.values() if isinstance(child, dict) else child:
                # Union choices can be `(schema, label)` tuples
                yield item[0] if isinstance(item, tuple) else item
        else:
            yield child


def _is_custom_serialization(serialization: object) -> bool:
    # Serializers that only run in JSON mode don't affect `model_dump()`
    return isinstance(serialization, dict) and serialization.get("when_used") not in ("json", "json-unless-none")


def _get_validation_paths(model: type[BaseModel], name: str, field: FieldInfo) -> list[list[Union[str, int]]]:
    """Return the paths in the input data that the field validates, in the order Pydantic tries them"""
    alias = field.validation_alias
    validate_by_alias, validate_by_name, _ = _get_validation_settings(model)
    paths: list[list[Union[str, int]]] = []
    if not validate_by_alias or alias is None:
        paths.append([name])
    else:
        # Pydantic only accepts strings, `AliasPath`, and `AliasChoices`, which this turns into lists of paths
        paths.extend(cast("list[list[Union[str, int]]]", _normalize_validation_alias(alias)))
    if validate_by_name:
        paths.append([name])
    return paths


def _get_validation_settings(model: type[BaseModel]) -> tuple[bool, bool, str]:
    """Return `(validate_by_alias, validate_by_name, extra)` of the model's config, resolved like pydantic does"""
    config = model.model_config
    validate_by_alias = config.get("validate_by_alias", True)
    validate_by_name = config.get("validate_by_name")
    if validate_by_name is None:
        populate_by_name = config.get("populate_by_name")
        validate_by_name = not validate_by_alias if populate_by_name is None else populate_by_name
    return validate_by_alias, validate_by_name, config.get("extra") or "ignore"


def _do_compiled_settings_match(model: type[BaseModel], compiled_config: object) -> bool:
    """Whether the compiled schema validates the model's keys with the settings of its config, which we plan with.

    A custom `__get_pydantic_core_schema__` can change them.
    """
    return isinstance(compiled_config, dict) and _get_validation_settings(model) == (
        compiled_config.get("validate_by_alias", True),
        compiled_config.get("validate_by_name", False),
        compiled_config.get("extra_fields_behavior", "ignore"),
    )


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
