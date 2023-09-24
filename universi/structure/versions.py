import functools
import inspect
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from enum import Enum
from typing import Any, ClassVar, ParamSpec, TypeAlias, TypeVar

from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi._compat import ModelField, _normalize_errors
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import solve_dependencies
from fastapi.exceptions import RequestValidationError
from fastapi.routing import _prepare_response_content
from pydantic import BaseModel
from typing_extensions import assert_never

from universi.exceptions import UniversiError, UniversiStructureError
from universi.structure.endpoints import AlterEndpointSubInstruction
from universi.structure.enums import AlterEnumSubInstruction

from .._utils import Sentinel
from .common import Endpoint, VersionDate, VersionedModel
from .data import (
    AlterRequestByPathInstruction,
    AlterRequestBySchemaInstruction,
    AlterResponseByPathInstruction,
    AlterResponseBySchemaInstruction,
    RequestInfo,
    ResponseInfo,
)
from .schemas import AlterSchemaInstruction, AlterSchemaSubInstruction, SchemaPropertyDefinitionInstruction

_UNIVERSI_REQUEST_PARAM_NAME = "universi_request_param"
_UNIVERSI_RESPONSE_PARAM_NAME = "universi_response_param"
_P = ParamSpec("_P")
_R = TypeVar("_R")
PossibleInstructions: TypeAlias = (
    AlterSchemaSubInstruction
    | AlterEndpointSubInstruction
    | AlterEnumSubInstruction
    | AlterSchemaInstruction
    | staticmethod
)


class VersionChange:
    description: ClassVar[str] = Sentinel
    instructions_to_migrate_to_previous_version: ClassVar[Sequence[PossibleInstructions]] = Sentinel
    alter_schema_instructions: ClassVar[list[AlterSchemaSubInstruction | AlterSchemaInstruction]] = Sentinel
    alter_enum_instructions: ClassVar[list[AlterEnumSubInstruction]] = Sentinel
    alter_endpoint_instructions: ClassVar[list[AlterEndpointSubInstruction]] = Sentinel
    alter_request_by_schema_instructions: ClassVar[dict[type[BaseModel], AlterRequestBySchemaInstruction]] = Sentinel
    alter_request_by_path_instructions: ClassVar[dict[str, list[AlterRequestByPathInstruction]]] = Sentinel
    alter_response_by_schema_instructions: ClassVar[dict[type, AlterResponseBySchemaInstruction]] = Sentinel
    alter_response_by_path_instructions: ClassVar[dict[str, list[AlterResponseByPathInstruction]]] = Sentinel
    _bound_version_bundle: "VersionBundle | None"

    def __init_subclass__(cls, _abstract: bool = False) -> None:
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
            if isinstance(instruction, SchemaPropertyDefinitionInstruction):
                cls.alter_schema_instructions.append(instruction)
            elif isinstance(instruction, AlterRequestBySchemaInstruction):
                if instruction.schema in cls.alter_request_by_schema_instructions:
                    raise UniversiStructureError(
                        f'There already exists a request migration for "{instruction.schema.__name__}" '
                        f'in "{cls.__name__}".',
                    )
                cls.alter_request_by_schema_instructions[instruction.schema] = instruction
            elif isinstance(instruction, AlterRequestByPathInstruction):
                cls.alter_request_by_path_instructions[instruction.path].append(instruction)
            elif isinstance(instruction, AlterResponseBySchemaInstruction):
                if instruction.schema in cls.alter_response_by_schema_instructions:
                    raise UniversiStructureError(
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
        cls.alter_endpoint_instructions = []
        cls.alter_request_by_schema_instructions = {}
        cls.alter_request_by_path_instructions = defaultdict(list)
        cls.alter_response_by_schema_instructions = {}
        cls.alter_response_by_path_instructions = defaultdict(list)
        for alter_instruction in cls.instructions_to_migrate_to_previous_version:
            if isinstance(alter_instruction, AlterSchemaSubInstruction | AlterSchemaInstruction):
                cls.alter_schema_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterEnumSubInstruction):
                cls.alter_enum_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterEndpointSubInstruction):
                cls.alter_endpoint_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, staticmethod):  # pragma: no cover
                raise NotImplementedError(f'"{alter_instruction}" is an unacceptable version change instruction')
            else:
                assert_never(alter_instruction)

    @classmethod
    def _validate_subclass(cls):
        if cls.description is Sentinel:
            raise UniversiStructureError(
                f"Version change description is not set on '{cls.__name__}' but is required.",
            )
        if cls.instructions_to_migrate_to_previous_version is Sentinel:
            raise UniversiStructureError(
                f"Attribute 'instructions_to_migrate_to_previous_version' is not set on '{cls.__name__}'"
                " but is required.",
            )
        if not isinstance(cls.instructions_to_migrate_to_previous_version, Sequence):
            raise UniversiStructureError(
                f"Attribute 'instructions_to_migrate_to_previous_version' must be a sequence in '{cls.__name__}'.",
            )
        for instruction in cls.instructions_to_migrate_to_previous_version:
            if not isinstance(instruction, PossibleInstructions):
                raise UniversiStructureError(
                    f"Instruction '{instruction}' is not allowed. Please, use the correct instruction types",
                )
        for attr_name, attr_value in cls.__dict__.items():
            if not isinstance(
                attr_value,
                SchemaPropertyDefinitionInstruction
                | AlterRequestBySchemaInstruction
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
                raise UniversiStructureError(
                    f"Found: '{attr_name}' attribute of type '{type(attr_value)}' in '{cls.__name__}'."
                    " Only migration instructions and schema properties are allowed in version change class body.",
                )

    @classmethod
    def _check_no_subclassing(cls):
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
            raise UniversiError(
                f"You tried to check whether '{cls.__name__}' is active but it was never bound to any version.",
            )
        api_version = cls._bound_version_bundle.api_version_var.get()
        if api_version is None:
            return True
        return cls._bound_version_bundle._version_changes_to_version_mapping[cls] <= api_version


class Version:
    def __init__(self, value: VersionDate, *version_changes: type[VersionChange]) -> None:
        self.value = value
        self.version_changes = version_changes

    def __repr__(self) -> str:
        return f"Version('{self.value}')"


class VersionBundle:
    def __init__(self, *versions: Version, api_version_var: ContextVar[VersionDate | None]) -> None:
        self.versions = versions
        self.api_version_var = api_version_var
        if sorted(versions, key=lambda v: v.value, reverse=True) != list(versions):
            raise ValueError(
                "Versions are not sorted correctly. Please sort them in descending order.",
            )
        if versions[-1].version_changes:
            raise UniversiStructureError(
                f'The first version "{versions[-1].value}" cannot have any version changes. '
                "Version changes are defined to migrate to/from a previous version so you "
                "cannot define one for the very first version.",
            )
        version_values = set()
        for version in versions:
            if version.value not in version_values:
                version_values.add(version.value)
            else:
                raise UniversiStructureError(
                    f"You tried to define two versions with the same value in the same "
                    f"{VersionBundle.__name__}: '{version.value}'.",
                )
            for version_change in version.version_changes:
                if version_change._bound_version_bundle is not None:
                    raise UniversiStructureError(
                        f"You tried to bind version change '{version_change.__name__}' to two different versions. "
                        "It is prohibited.",
                    )
                version_change._bound_version_bundle = self

    def __iter__(self):
        yield from self.versions

    @functools.cached_property
    def versioned_schemas(self) -> dict[str, type[VersionedModel]]:
        return {
            f"{instruction.schema.__module__}.{instruction.schema.__name__}": instruction.schema
            for version in self.versions
            for version_change in version.version_changes
            for instruction in list(version_change.alter_schema_instructions)
            + list(version_change.alter_request_by_schema_instructions.values())
        }

    @functools.cached_property
    def versioned_enums(self) -> dict[str, type[Enum]]:
        return {
            f"{instruction.enum.__module__}.{instruction.enum.__name__}": instruction.enum
            for version in self.versions
            for version_change in version.version_changes
            for instruction in version_change.alter_enum_instructions
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
        dependant: Dependant,
        request: FastapiRequest,
        response: FastapiResponse,
        request_info: RequestInfo,
        current_version: VersionDate,
    ):
        path = request.scope["path"]
        method = request.method
        for v in reversed(self.versions):
            if v.value <= current_version:
                continue
            for version_change in v.version_changes:
                if body_type is not None and body_type in version_change.alter_request_by_schema_instructions:
                    version_change.alter_request_by_schema_instructions[body_type](request_info)
                if path in version_change.alter_request_by_path_instructions:
                    for instruction in version_change.alter_request_by_path_instructions[path]:
                        if method in instruction.methods:
                            instruction(request_info)
        # TODO: Consider finding all private attributes and automatically assigning them to body after
        # solving dependencies. Not sure if it's a good idea though.
        # TASK: https://github.com/Ovsyanka83/universi/issues/51
        request.scope["headers"] = tuple((key.encode(), value.encode()) for key, value in request_info.headers.items())
        del request._headers
        new_kwargs, errors, _, _, _ = await solve_dependencies(
            request=request,
            response=response,
            dependant=dependant,
            body=request_info.body,
            # TODO: Take it from route
            dependency_overrides_provider=None,
        )

        if errors:
            raise RequestValidationError(_normalize_errors(errors), body=request_info.body)
        return new_kwargs

    def _migrate_response(
        self,
        response_info: ResponseInfo,
        current_version: VersionDate,
        latest_response_model: Any | None,
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
                    latest_response_model
                    and latest_response_model in version_change.alter_response_by_schema_instructions
                ):
                    version_change.alter_response_by_schema_instructions[latest_response_model](response_info)
                if path in version_change.alter_response_by_path_instructions:
                    for instruction in version_change.alter_response_by_path_instructions[path]:
                        if method in instruction.methods:
                            instruction(response_info)
        return response_info

    # TODO: This function is all over the place. Refactor it and all functions it calls.
    def _versioned(
        self,
        template_module_body_field_for_request_migrations: type[BaseModel] | None,
        module_body_field_name: str | None,
        body_params: list[ModelField],
        dependant_for_request_migrations: Dependant,
        latest_response_model: Any,
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
                    is_single_body_field=len(body_params) == 1,
                )

                return await self._convert_endpoint_response_to_version(
                    endpoint,  # pyright: ignore[reportGeneralTypeIssues]
                    latest_response_model,
                    path,
                    method,
                    response_param_name,
                    kwargs,
                    response,
                )

            if request_param_name == _UNIVERSI_REQUEST_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _UNIVERSI_REQUEST_PARAM_NAME, FastapiRequest)
            if response_param_name == _UNIVERSI_RESPONSE_PARAM_NAME:
                _add_keyword_only_parameter(decorator, _UNIVERSI_RESPONSE_PARAM_NAME, FastapiResponse)

            return decorator  # pyright: ignore[reportGeneralTypeIssues]

        return wrapper

    async def _convert_endpoint_response_to_version(
        self,
        func_to_get_response_from: Endpoint,
        latest_response_model: Any,
        path: str,
        method: str,
        response_param_name: str,
        kwargs: dict[str, Any],
        fastapi_response_dependency: FastapiResponse,
    ) -> Any:
        if response_param_name == _UNIVERSI_RESPONSE_PARAM_NAME:
            kwargs.pop(response_param_name)
        # TODO: Verify that we handle fastapi.Response here
        # TODO: Verify that we handle fastapi.Response descendants
        response_or_response_body: FastapiResponse | object = await func_to_get_response_from(**kwargs)
        api_version = self.api_version_var.get()
        if api_version is None:
            return response_or_response_body
        if isinstance(response_or_response_body, FastapiResponse):
            response_info = ResponseInfo(
                response_or_response_body,
                # TODO: Give user the ability to specify their own renderer
                # TODO: Only do this if there are migrations
                json.loads(response_or_response_body.body) if response_or_response_body.body else None,
            )
        else:
            # TODO: We probably need to call this in the same way as in fastapi instead of hardcoding exclude_unset.
            # We have such an ability if we force passing the route into this wrapper. Or maybe not... Important!
            response_info = ResponseInfo(
                fastapi_response_dependency,
                _prepare_response_content(response_or_response_body, exclude_unset=False),
            )

        response_info = self._migrate_response(
            response_info,
            api_version,
            latest_response_model,
            path,
            method,
        )
        if isinstance(response_or_response_body, FastapiResponse):
            # TODO: Give user the ability to specify their own renderer
            # TODO: Only do this if there are migrations
            response_info._response.body = json.dumps(response_info.body).encode()
            return response_info._response
        return response_info.body

    async def _convert_endpoint_kwargs_to_version(
        self,
        template_module_body_field_for_request_migrations: type[BaseModel] | None,
        body_field_alias: str | None,
        dependant_of_version_to_which_we_ultimately_migrate_request: Dependant,
        request_param_name: str,
        kwargs: dict[str, Any],
        response: FastapiResponse,
        *,
        is_single_body_field: bool,
    ):
        request: FastapiRequest = kwargs[request_param_name]
        if request_param_name == _UNIVERSI_REQUEST_PARAM_NAME:
            kwargs.pop(request_param_name)

        api_version = self.api_version_var.get()
        if api_version is None:
            return kwargs

        if (
            is_single_body_field
            and template_module_body_field_for_request_migrations is not None
            and body_field_alias is not None
        ):
            # TODO: What if the user never edits it? We just add a round of (de)serialization

            raw_body = kwargs[body_field_alias]
            if raw_body is None:
                body = None
            else:
                body = raw_body.dict(by_alias=True)
                if kwargs[body_field_alias].__custom_root_type__:
                    body = body["__root__"]
        else:
            # TODO: Add a test that checks that this is even possible with a form or a file.
            # TODO: What if it's large? We need to also make ours a generator, then... But we can't because ours is
            # synchronous. HMM... Or maybe just reading it later will solve the issue. Who knows...
            body = await request.body()
        request_info = RequestInfo(request, body)
        new_kwargs = await self._migrate_request(
            template_module_body_field_for_request_migrations,
            dependant_of_version_to_which_we_ultimately_migrate_request,
            request,
            response,
            request_info,
            api_version,
        )
        # Because we re-added it into our kwargs when we did solve_dependencies
        if request_param_name == _UNIVERSI_REQUEST_PARAM_NAME:
            new_kwargs.pop(request_param_name)

        return new_kwargs


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
