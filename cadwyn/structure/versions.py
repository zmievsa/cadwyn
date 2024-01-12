import email.message
import functools
import inspect
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from contextvars import ContextVar
from enum import Enum
from types import ModuleType
from typing import Any, ClassVar, ParamSpec, TypeAlias, TypeVar, cast

from fastapi import HTTPException, params
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi._compat import ModelField, _normalize_errors
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.routing import APIRoute, _prepare_response_content
from pydantic import BaseModel
from starlette._utils import is_async_callable
from typing_extensions import assert_never

from cadwyn._compat import Undefined
from cadwyn._package_utils import IdentifierPythonPath, get_cls_pythonpath
from cadwyn.exceptions import CadwynError, CadwynStructureError

from .._utils import Sentinel
from .common import Endpoint, VersionDate, VersionedModel
from .data import (
    _EMPTY_PRELOADED_BODY,
    AlterRequestByPathInstruction,
    AlterRequestBySchemaInstruction,
    AlterResponseByPathInstruction,
    AlterResponseBySchemaInstruction,
    RequestInfo,
    ResponseInfo,
    _LazyBody,
)
from .endpoints import AlterEndpointSubInstruction
from .enums import AlterEnumSubInstruction
from .modules import AlterModuleInstruction
from .schemas import AlterSchemaInstruction, AlterSchemaSubInstruction

_CADWYN_REQUEST_PARAM_NAME = "cadwyn_request_param"
_CADWYN_RESPONSE_PARAM_NAME = "cadwyn_response_param"
_P = ParamSpec("_P")
_R = TypeVar("_R")
PossibleInstructions: TypeAlias = (
    AlterSchemaSubInstruction
    | AlterEndpointSubInstruction
    | AlterEnumSubInstruction
    | AlterSchemaInstruction
    | AlterModuleInstruction
    | staticmethod
)
APIVersionVarType: TypeAlias = ContextVar[VersionDate | None] | ContextVar[VersionDate]


class VersionChange:
    description: ClassVar[str] = Sentinel
    instructions_to_migrate_to_previous_version: ClassVar[Sequence[PossibleInstructions]] = Sentinel
    alter_schema_instructions: ClassVar[list[AlterSchemaSubInstruction | AlterSchemaInstruction]] = Sentinel
    alter_enum_instructions: ClassVar[list[AlterEnumSubInstruction]] = Sentinel
    alter_module_instructions: ClassVar[list[AlterModuleInstruction]] = Sentinel
    alter_endpoint_instructions: ClassVar[list[AlterEndpointSubInstruction]] = Sentinel
    alter_request_by_schema_instructions: ClassVar[dict[type[BaseModel], AlterRequestBySchemaInstruction]] = Sentinel
    alter_request_by_path_instructions: ClassVar[dict[str, list[AlterRequestByPathInstruction]]] = Sentinel
    alter_response_by_schema_instructions: ClassVar[dict[type, AlterResponseBySchemaInstruction]] = Sentinel
    alter_response_by_path_instructions: ClassVar[dict[str, list[AlterResponseByPathInstruction]]] = Sentinel
    _bound_version_bundle: "VersionBundle | None"

    def __init_subclass__(cls, _abstract: bool = False) -> None:
        super().__init_subclass__()

        if _abstract:
            return
        cls._validate_subclass()
        cls._extract_list_instructions_into_correct_containers()
        cls._extract_body_instructions_into_correct_containers()
        cls._check_no_subclassing()
        cls._bound_version_bundle = None

    @classmethod
    def _extract_body_instructions_into_correct_containers(cls):
        for instruction in cls.__dict__.values():
            if isinstance(instruction, AlterRequestBySchemaInstruction):
                if instruction.schema in cls.alter_request_by_schema_instructions:
                    raise CadwynStructureError(
                        f'There already exists a request migration for "{instruction.schema.__name__}" '
                        f'in "{cls.__name__}".',
                    )
                cls.alter_request_by_schema_instructions[instruction.schema] = instruction
            elif isinstance(instruction, AlterRequestByPathInstruction):
                cls.alter_request_by_path_instructions[instruction.path].append(instruction)
            elif isinstance(instruction, AlterResponseBySchemaInstruction):
                if instruction.schema in cls.alter_response_by_schema_instructions:
                    raise CadwynStructureError(
                        f'There already exists a response migration for "{instruction.schema.__name__}" '
                        f'in "{cls.__name__}".',
                    )
                cls.alter_response_by_schema_instructions[instruction.schema] = instruction
            elif isinstance(instruction, AlterResponseByPathInstruction):
                cls.alter_response_by_path_instructions[instruction.path].append(instruction)

    @classmethod
    def _extract_list_instructions_into_correct_containers(cls):
        cls.alter_schema_instructions = []
        cls.alter_enum_instructions = []
        cls.alter_module_instructions = []
        cls.alter_endpoint_instructions = []
        cls.alter_request_by_schema_instructions = {}
        cls.alter_request_by_path_instructions = defaultdict(list)
        cls.alter_response_by_schema_instructions = {}
        cls.alter_response_by_path_instructions = defaultdict(list)
        for alter_instruction in cls.instructions_to_migrate_to_previous_version:
            if isinstance(alter_instruction, AlterSchemaInstruction | AlterSchemaSubInstruction):
                cls.alter_schema_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterEnumSubInstruction):
                cls.alter_enum_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterModuleInstruction):
                cls.alter_module_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterEndpointSubInstruction):
                cls.alter_endpoint_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, staticmethod):  # pragma: no cover
                raise NotImplementedError(f'"{alter_instruction}" is an unacceptable version change instruction')
            else:
                assert_never(alter_instruction)

    @classmethod
    def _validate_subclass(cls):
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
            if not isinstance(instruction, PossibleInstructions):
                raise CadwynStructureError(
                    f"Instruction '{instruction}' is not allowed. Please, use the correct instruction types",
                )
        for attr_name, attr_value in cls.__dict__.items():
            if not isinstance(
                attr_value,
                AlterRequestBySchemaInstruction
                | AlterRequestByPathInstruction
                | AlterResponseBySchemaInstruction
                | AlterResponseByPathInstruction,
            ) and attr_name not in {
                "description",
                "side_effects",
                "instructions_to_migrate_to_previous_version",
                "__module__",
                "__doc__",
            }:
                raise CadwynStructureError(
                    f"Found: '{attr_name}' attribute of type '{type(attr_value)}' in '{cls.__name__}'."
                    " Only migration instructions and schema properties are allowed in version change class body.",
                )

    @classmethod
    def _check_no_subclassing(cls):
        if cls.mro() != [cls, VersionChange, object]:
            raise TypeError(
                f"Can't subclass {cls.__name__} as it was never meant to be subclassed.",
            )

    def __init__(self) -> None:  # pyright: ignore[reportMissingSuperCall]
        raise TypeError(
            f"Can't instantiate {self.__class__.__name__} as it was never meant to be instantiated.",
        )


class VersionChangeWithSideEffects(VersionChange, _abstract=True):
    @classmethod
    def _check_no_subclassing(cls):
        if cls.mro() != [cls, VersionChangeWithSideEffects, VersionChange, object]:
            raise TypeError(
                f"Can't subclass {cls.__name__} as it was never meant to be subclassed.",
            )

    @classmethod
    @property
    def is_applied(cls) -> bool:
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
    def __init__(self, value: VersionDate, *version_changes: type[VersionChange]) -> None:
        super().__init__()

        self.value = value
        self.version_changes = version_changes

    def __repr__(self) -> str:
        return f"Version('{self.value}')"


class VersionBundle:
    def __init__(
        self,
        latest_version: Version,
        /,
        *other_versions: Version,
        api_version_var: APIVersionVarType | None = None,
    ) -> None:
        super().__init__()

        self.versions = (latest_version, *other_versions)
        if api_version_var is None:
            api_version_var = ContextVar("cadwyn_api_version")
        self.api_version_var = api_version_var
        if sorted(self.versions, key=lambda v: v.value, reverse=True) != list(self.versions):
            raise CadwynStructureError(
                "Versions are not sorted correctly. Please sort them in descending order.",
            )
        if self.versions[-1].version_changes:
            raise CadwynStructureError(
                f'The first version "{self.versions[-1].value}" cannot have any version changes. '
                "Version changes are defined to migrate to/from a previous version so you "
                "cannot define one for the very first version.",
            )
        version_values = set()
        for version in self.versions:
            if version.value not in version_values:
                version_values.add(version.value)
            else:
                raise CadwynStructureError(
                    f"You tried to define two versions with the same value in the same "
                    f"{VersionBundle.__name__}: '{version.value}'.",
                )
            for version_change in version.version_changes:
                if version_change._bound_version_bundle is not None:
                    raise CadwynStructureError(
                        f"You tried to bind version change '{version_change.__name__}' to two different versions. "
                        "It is prohibited.",
                    )
                version_change._bound_version_bundle = self

    def __iter__(self):
        yield from self.versions

    @functools.cached_property
    def versioned_schemas(self) -> dict[IdentifierPythonPath, type[VersionedModel]]:
        return {
            get_cls_pythonpath(instruction.schema): instruction.schema
            for version in self.versions
            for version_change in version.version_changes
            for instruction in list(version_change.alter_schema_instructions)
            + list(version_change.alter_request_by_schema_instructions.values())
        }

    @functools.cached_property
    def versioned_enums(self) -> dict[IdentifierPythonPath, type[Enum]]:
        return {
            get_cls_pythonpath(instruction.enum): instruction.enum
            for version in self.versions
            for version_change in version.version_changes
            for instruction in version_change.alter_enum_instructions
        }

    @functools.cached_property
    def versioned_modules(self) -> dict[IdentifierPythonPath, ModuleType]:
        return {
            # We do this because when users import their modules, they might import
            # the __init__.py file directly instead of the package itself
            # which results in this extra `.__init__` suffix in the name
            instruction.module.__name__.removesuffix(".__init__"): instruction.module
            for version in self.versions
            for version_change in version.version_changes
            for instruction in version_change.alter_module_instructions
        }

    @functools.cached_property
    def _version_changes_to_version_mapping(
        self,
    ) -> dict[type[VersionChange], VersionDate]:
        return {
            version_change: version.value for version in self.versions for version_change in version.version_changes
        }

    async def _migrate_request(
        self,
        body_type: type[BaseModel] | None,
        latest_dependant_with_internal_schema: Dependant,
        request: FastapiRequest,
        response: FastapiResponse,
        request_info: RequestInfo,
        current_version: VersionDate,
        latest_route: APIRoute,
        old_kwargs: dict[str, Any],
    ):
        path = request.scope["path"]
        method = request.method
        at_least_one_migration_was_applied = False
        for v in reversed(self.versions):
            if v.value <= current_version:
                continue
            for version_change in v.version_changes:
                if body_type is not None and body_type in version_change.alter_request_by_schema_instructions:
                    at_least_one_migration_was_applied = True
                    version_change.alter_request_by_schema_instructions[body_type](request_info)
                if path in version_change.alter_request_by_path_instructions:
                    for instruction in version_change.alter_request_by_path_instructions[path]:
                        if method in instruction.methods:
                            at_least_one_migration_was_applied = True
                            instruction(request_info)
        if not at_least_one_migration_was_applied and (
            # Either if:
            # * there is no body
            # * if there is no body with internal parameters
            latest_route.body_field is None
            or (
                latest_route.dependant.body_params
                and latest_route.dependant.body_params[0].type_
                == latest_dependant_with_internal_schema.body_params[0].type_
            )
        ):
            return old_kwargs
        request.scope["headers"] = tuple((key.encode(), value.encode()) for key, value in request_info.headers.items())
        del request._headers
        # Remember this: if len(body_params) == 1, then route.body_schema == route.dependant.body_params[0]
        new_kwargs, errors, _, _, _ = await solve_dependencies(
            request=request,
            response=response,
            dependant=latest_dependant_with_internal_schema,
            body=request_info.body,
            dependency_overrides_provider=latest_route.dependency_overrides_provider,
        )
        if errors:
            raise RequestValidationError(_normalize_errors(errors), body=request_info.body)
        return new_kwargs

    def _migrate_response(
        self,
        response_info: ResponseInfo,
        current_version: VersionDate,
        latest_route: APIRoute,
        path: str,
        method: str,
    ) -> ResponseInfo:
        """Convert the data to a specific version by applying all version changes in reverse order.

        Args:
            endpoint: the function which usually returns this data. Data migrations marked with this endpoint will
            be applied to the passed data
            payload: data to be migrated. Will be mutated during the call
            version: the version to which the data should be converted

        Returns:
            Modified data
        """

        for v in self.versions:
            if v.value <= current_version:
                break
            for version_change in v.version_changes:
                if (
                    latest_route.response_model
                    and latest_route.response_model in version_change.alter_response_by_schema_instructions
                ):
                    version_change.alter_response_by_schema_instructions[latest_route.response_model](response_info)
                if path in version_change.alter_response_by_path_instructions:
                    for instruction in version_change.alter_response_by_path_instructions[path]:
                        if method in instruction.methods:
                            instruction(response_info)
        return response_info

    # TODO (https://github.com/zmievsa/cadwyn/issues/113): Refactor this function and all functions it calls.
    def _versioned(
        self,
        template_module_body_field_for_request_migrations: type[BaseModel] | None,
        module_body_field_name: str | None,
        route: APIRoute,
        latest_route: APIRoute,
        dependant_for_request_migrations: Dependant,
        *,
        request_param_name: str,
        response_param_name: str,
    ) -> Callable[[Endpoint[_P, _R]], Endpoint[_P, _R]]:
        def wrapper(endpoint: Endpoint[_P, _R]) -> Endpoint[_P, _R]:
            @functools.wraps(endpoint)
            async def decorator(*args: Any, **kwargs: Any) -> _R:
                request: FastapiRequest = kwargs[request_param_name]
                response: FastapiResponse = kwargs[response_param_name]
                path = request.scope["path"]
                method = request.method
                kwargs = await self._convert_endpoint_kwargs_to_version(
                    template_module_body_field_for_request_migrations,
                    module_body_field_name,
                    # Dependant must be from the version of the finally migrated request, not the version of endpoint
                    dependant_for_request_migrations,
                    request_param_name,
                    kwargs,
                    response,
                    route,
                    latest_route,
                )

                return await self._convert_endpoint_response_to_version(
                    endpoint,
                    latest_route,
                    path,
                    method,
                    response_param_name,
                    kwargs,
                    response,
                )

            if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _CADWYN_REQUEST_PARAM_NAME, FastapiRequest)
            if response_param_name == _CADWYN_RESPONSE_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _CADWYN_RESPONSE_PARAM_NAME, FastapiResponse)

            return decorator  # pyright: ignore[reportGeneralTypeIssues]

        return wrapper

    async def _convert_endpoint_response_to_version(
        self,
        func_to_get_response_from: Endpoint,
        latest_route: APIRoute,
        path: str,
        method: str,
        response_param_name: str,
        kwargs: dict[str, Any],
        fastapi_response_dependency: FastapiResponse,
    ) -> Any:
        if response_param_name == _CADWYN_RESPONSE_PARAM_NAME:
            kwargs.pop(response_param_name)
        if is_async_callable(func_to_get_response_from):
            response_or_response_body: FastapiResponse | object = await func_to_get_response_from(**kwargs)
        else:
            response_or_response_body: FastapiResponse | object = await run_in_threadpool(
                func_to_get_response_from,
                **kwargs,
            )
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
                body = json.loads(response_or_response_body.body)
            else:
                body = None
                # TODO (https://github.com/zmievsa/cadwyn/issues/51): Only do this if there are migrations
            response_info = ResponseInfo(response_or_response_body, body)
        else:
            response_info = ResponseInfo(
                fastapi_response_dependency,
                _prepare_response_content(
                    response_or_response_body,
                    exclude_unset=latest_route.response_model_exclude_unset,
                    exclude_defaults=latest_route.response_model_exclude_defaults,
                    exclude_none=latest_route.response_model_exclude_none,
                ),
            )

        response_info = self._migrate_response(
            response_info,
            api_version,
            latest_route,
            path,
            method,
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
                response_info._response.body = json.dumps(response_info.body).encode()
            return response_info._response
        return response_info.body

    async def _convert_endpoint_kwargs_to_version(
        self,
        template_module_body_field_for_request_migrations: type[BaseModel] | None,
        body_field_alias: str | None,
        latest_dependant_with_internal_schema: Dependant,
        request_param_name: str,
        kwargs: dict[str, Any],
        response: FastapiResponse,
        route: APIRoute,
        latest_route: APIRoute,
    ):
        request: FastapiRequest = kwargs[request_param_name]
        if request_param_name == _CADWYN_REQUEST_PARAM_NAME:
            kwargs.pop(request_param_name)

        api_version = self.api_version_var.get()
        if api_version is None:
            return kwargs

        # This is a kind of body param you get when you define a single pydantic schema in your route's body
        there_is_a_single_simple_body_param = (
            len(route.dependant.body_params) == 1
            and template_module_body_field_for_request_migrations is not None
            and body_field_alias is not None
            and body_field_alias in kwargs
        )
        if there_is_a_single_simple_body_param:
            preloaded_body = _EMPTY_PRELOADED_BODY
        else:
            preloaded_body = await _get_body(request, route.body_field)

        request_info = RequestInfo(
            request,
            _LazyBody(
                preloaded_body,
                validated_route_parameters=kwargs,
                body_field_alias=body_field_alias,
                body_field=route.body_field,
            ),
        )
        new_kwargs = await self._migrate_request(
            template_module_body_field_for_request_migrations,
            latest_dependant_with_internal_schema,
            request,
            response,
            request_info,
            api_version,
            latest_route,
            kwargs,
        )
        # Because we re-added it into our kwargs when we did solve_dependencies
        if _CADWYN_REQUEST_PARAM_NAME in new_kwargs:
            new_kwargs.pop(_CADWYN_REQUEST_PARAM_NAME)

        return new_kwargs


# We use this instead of `.body()` to automatically guess body type and load the correct body, even if it's a form
async def _get_body(request: FastapiRequest, body_field: ModelField | None):  # pragma: no cover # This is from fastapi
    is_body_form = body_field and isinstance(body_field.field_info, params.Form)
    try:
        body: Any = None
        if body_field:
            if is_body_form:
                body = await request.form()
                stack = cast(AsyncExitStack, request.scope.get("fastapi_astack"))
                stack.push_async_callback(body.close)
            else:
                body_bytes = await request.body()
                if body_bytes:
                    json_body: Any = Undefined
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
                    if json_body != Undefined:
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
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="There was an error parsing the body") from e
    return body


def _add_keyword_only_parameter(
    func: Callable,
    param_name: str,
    param_annotation: type,
):
    signature = inspect.signature(func)
    func.__signature__ = signature.replace(
        parameters=(
            [
                *list(signature.parameters.values()),
                inspect.Parameter(param_name, kind=inspect._ParameterKind.KEYWORD_ONLY, annotation=param_annotation),
            ]
        ),
    )
