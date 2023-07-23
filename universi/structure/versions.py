import datetime
import functools
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from enum import Enum
from typing import Any, ClassVar, ParamSpec, TypeAlias, TypeVar

from fastapi.routing import _prepare_response_content
from typing_extensions import assert_never

from universi.exceptions import UniversiError, UniversiStructureError
from universi.header import api_version_var
from universi.structure.endpoints import AlterEndpointSubInstruction
from universi.structure.enums import AlterEnumSubInstruction

from .._utils import Sentinel
from .common import Endpoint, VersionedModel
from .responses import AlterResponseInstruction
from .schemas import AlterSchemaSubInstruction, SchemaPropertyDefinitionInstruction

_P = ParamSpec("_P")
_R = TypeVar("_R")
VersionDate: TypeAlias = datetime.date
PossibleInstructions: TypeAlias = AlterSchemaSubInstruction | AlterEndpointSubInstruction | AlterEnumSubInstruction


class VersionChange:
    description: ClassVar[str] = Sentinel
    instructions_to_migrate_to_previous_version: ClassVar[Sequence[PossibleInstructions]] = Sentinel
    alter_schema_instructions: ClassVar[Sequence[AlterSchemaSubInstruction]] = Sentinel
    alter_enum_instructions: ClassVar[Sequence[AlterEnumSubInstruction]] = Sentinel
    alter_endpoint_instructions: ClassVar[Sequence[AlterEndpointSubInstruction]] = Sentinel
    alter_response_instructions: ClassVar[dict[Endpoint, AlterResponseInstruction]] = Sentinel
    _bound_versions: "VersionBundle | None"

    def __init_subclass__(cls, _abstract: bool = False) -> None:
        if _abstract:
            return
        if cls.description is Sentinel:
            raise UniversiStructureError(
                f"Version change description is not set on '{cls.__name__}' but is required.",
            )
        if cls.instructions_to_migrate_to_previous_version is Sentinel:
            raise UniversiStructureError(
                f"Attribute 'instructions_to_migrate_to_previous_version' is not set on '{cls.__name__}' but is required.",
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
                AlterResponseInstruction | SchemaPropertyDefinitionInstruction,
            ) and attr_name not in {
                "description",
                "side_effects",
                "instructions_to_migrate_to_previous_version",
                "__module__",
                "__doc__",
            }:
                raise UniversiStructureError(
                    f"Found: '{attr_name}' attribute of type '{type(attr_value)}' in '{cls.__name__}'. Only migration instructions and schema properties are allowed in version change class body.",
                )

        cls.alter_schema_instructions = []
        cls.alter_enum_instructions = []
        cls.alter_endpoint_instructions = []
        for alter_instruction in cls.instructions_to_migrate_to_previous_version:
            if isinstance(alter_instruction, AlterSchemaSubInstruction):
                cls.alter_schema_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterEnumSubInstruction):
                cls.alter_enum_instructions.append(alter_instruction)
            elif isinstance(alter_instruction, AlterEndpointSubInstruction):
                cls.alter_endpoint_instructions.append(alter_instruction)
            else:
                assert_never(alter_instruction)
        for value in cls.__dict__.values():
            if isinstance(value, SchemaPropertyDefinitionInstruction):
                cls.alter_schema_instructions.append(value)
        # TODO: You can include it in a for loop over dict above. Do so
        cls.alter_response_instructions = {
            endpoint: instruction
            for instruction in cls.__dict__.values()
            if isinstance(instruction, AlterResponseInstruction)
            for endpoint in instruction.endpoints
        }

        cls._check_no_subclassing()
        cls._bound_versions = None

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
    def is_active(cls) -> bool:
        if cls._bound_versions is None or cls not in cls._bound_versions._version_changes_to_version_mapping:
            raise UniversiError(
                f"You tried to check whether '{cls.__name__}' is active but it was never bound to any version.",
            )
        api_version = cls._bound_versions.api_version_var.get()
        if api_version is None:
            return True
        return cls._bound_versions._version_changes_to_version_mapping[cls] <= api_version


class Version:
    def __init__(
        self,
        date: VersionDate,
        *version_changes: type[VersionChange],
    ) -> None:
        self.date = date
        self.version_changes = version_changes


class VersionBundle:
    def __init__(
        self,
        *versions: Version,
        api_version_var: ContextVar[VersionDate | None] = api_version_var,
    ) -> None:
        self.versions = versions
        self.api_version_var = api_version_var
        if sorted(versions, key=lambda v: v.date, reverse=True) != list(versions):
            raise ValueError(
                "Versions are not sorted correctly. Please sort them in descending order.",
            )
        for version in versions:
            for version_change in version.version_changes:
                if version_change._bound_versions is not None:
                    raise UniversiStructureError(
                        f"You tried to bind version change '{version_change.__name__}' to two different versions. "
                        "It is prohibited.",
                    )
                version_change._bound_versions = self

    @functools.cached_property
    def versioned_schemas(self) -> dict[str, type[VersionedModel]]:
        return {
            instruction.schema.__module__ + instruction.schema.__name__: instruction.schema
            for version in self.versions
            for version_change in version.version_changes
            for instruction in version_change.alter_schema_instructions
        }

    @functools.cached_property
    def versioned_enums(self) -> dict[str, type[Enum]]:
        return {
            instruction.enum.__module__ + instruction.enum.__name__: instruction.enum
            for version in self.versions
            for version_change in version.version_changes
            for instruction in version_change.alter_enum_instructions
        }

    @functools.cached_property
    def _version_changes_to_version_mapping(
        self,
    ) -> dict[type[VersionChange], VersionDate]:
        return {version_change: version.date for version in self.versions for version_change in version.version_changes}

    # TODO: It might need caching for iteration to speed it up
    def data_to_version(
        self,
        endpoint: Endpoint,
        data: dict[str, Any],
        version: VersionDate,
    ) -> dict[str, Any]:
        """Convert the data to a specific version by applying all version changes in reverse order.

        Args:
            endpoint: the function which usually returns this data. Data migrations marked with this endpoint will
            be applied to the passed data
            data: data to be migrated. Will be mutated during the call
            version: the version to which the data should be converted

        Returns:
            Modified data
        """
        for v in self.versions:
            if v.date <= version:
                break
            for version_change in v.version_changes:
                if endpoint in version_change.alter_response_instructions:
                    version_change.alter_response_instructions[endpoint](data)
        return data

    def versioned(
        self,
        endpoint: Endpoint | None = None,
    ) -> Callable[[Endpoint[_P, _R]], Endpoint[_P, _R]]:
        def wrapper(func: Endpoint[_P, _R]) -> Endpoint[_P, _R]:
            @functools.wraps(func)
            async def decorator(*args: _P.args, **kwargs: _P.kwargs) -> _R:
                return await self._convert_endpoint_response_to_version(
                    func,
                    endpoint or func,
                    args,
                    kwargs,
                )

            decorator.func = func  # pyright: ignore[reportGeneralTypeIssues]
            return decorator

        return wrapper

    async def _convert_endpoint_response_to_version(
        self,
        func_to_get_response_from: Endpoint,
        endpoint: Endpoint,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        response = await func_to_get_response_from(*args, **kwargs)
        api_version = self.api_version_var.get()
        if api_version is None:
            return response
        # TODO We probably need to call this in the same way as in fastapi instead of hardcoding exclude_unset.
        # We have such an ability if we force passing the route into this wrapper. Or maybe not... Important!
        response = _prepare_response_content(response, exclude_unset=False)
        return self.data_to_version(endpoint, response, api_version)
