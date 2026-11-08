from collections import defaultdict
from collections.abc import (
    Sequence,
)
from contextvars import ContextVar
from datetime import date
from typing import (
    TYPE_CHECKING,
    ClassVar,
    TypeAlias,
    Union,
    assert_never,
    get_args,
)

from pydantic import BaseModel
from typing_extensions import override

from cadwyn._utils import (
    Sentinel,
    classproperty,
)
from cadwyn.exceptions import (
    CadwynError,
    CadwynStructureError,
)
from cadwyn.structure.common import VersionType
from cadwyn.structure.data import (
    _AlterRequestByPathInstruction,
    _AlterRequestBySchemaInstruction,
    _AlterResponseByPathInstruction,
    _AlterResponseBySchemaInstruction,
)
from cadwyn.structure.endpoints import AlterEndpointSubInstruction
from cadwyn.structure.enums import AlterEnumSubInstruction
from cadwyn.structure.schemas import AlterSchemaSubInstruction, SchemaHadInstruction

if TYPE_CHECKING:
    from ._bundle import VersionBundle
from ._common import _RouteId

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


def get_cls_pythonpath(cls: type) -> IdentifierPythonPath:
    return f"{cls.__module__}.{cls.__name__}"
