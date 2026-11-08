import copy
import dataclasses
import email.message
import functools
import http
import inspect
import itertools
import json
import os
import re
import tempfile
import threading
import uuid
import warnings
import weakref
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import (
    AsyncIterator,
    Callable,
    Coroutine,
    Hashable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack
from contextvars import ContextVar, copy_context
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, Flag
from logging import getLogger
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    BinaryIO,
    ClassVar,
    NamedTuple,
    NoReturn,
    TypeAlias,
    Union,
    assert_never,
    cast,
    get_args,
    get_origin,
)

import anyio
import fastapi.dependencies.utils
from fastapi import BackgroundTasks, HTTPException, params
from fastapi import Request as FastapiRequest
from fastapi import Response as FastapiResponse
from fastapi._compat import (
    ModelField,
    is_bytes_or_nonable_bytes_annotation,
    is_bytes_sequence_annotation,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, get_validation_alias, solve_dependencies
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute, _get_scope_effective_route_context
from pydantic import AliasChoices, AliasPath, BaseModel, BeforeValidator, RootModel
from pydantic.fields import FieldInfo
from pydantic_core import (
    PydanticCustomError,
    PydanticSerializationError,
    PydanticUndefined,
    SchemaSerializer,
    SchemaValidator,
    TzInfo,
    core_schema,
    from_json,
    to_json,
)
from starlette._utils import is_async_callable
from starlette.datastructures import FormData, UploadFile
from typing_extensions import ParamSpec, TypeVar, override
from typing_inspection.typing_objects import is_typealiastype

from cadwyn._internal.context_vars import CURRENT_DEPENDENCY_SOLVER_VAR
from cadwyn._utils import (
    UnionType,
    _get_type_alias_value,
    classproperty,
    get_member_with_same_value,
    is_enum_looked_up_by_value,
    is_same_value,
    lenient_issubclass,
)
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
_CADWYN_BACKGROUND_TASKS_PARAM_NAME = "cadwyn_background_tasks_param"
_RUN_BACKGROUND_TASKS_FLAG = "run_background_tasks_of_latest_version_dependencies"
_logger = getLogger(__name__)
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
# Since FastAPI 0.158.2, FastAPI looks the dict body of a form model up under the param's aliases. We use its private
# functions for picking form models and their aliases, so that we pick exactly the same ones
_FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS = tuple(map(int, re.findall(r"\d+", fastapi.__version__)[:3])) >= (0, 158, 2)
_get_form_model = cast(
    "Callable[[ModelField], Union[type[BaseModel], None]]",
    getattr(fastapi.dependencies.utils, "_get_form_model", None),
)
_get_param_aliases = cast(
    "Callable[[ModelField], list[tuple[str, str]]]", getattr(fastapi.dependencies.utils, "_get_param_aliases", None)
)
_get_cached_model_fields = cast(
    "Callable[[type[BaseModel]], list[ModelField]]",
    getattr(fastapi.dependencies.utils, "get_cached_model_fields", None),
)
# Since FastAPI 0.159.1, FastAPI checks with this function whether the form supplies an optional form model
_is_form_model_supplied = cast(
    "Union[Callable[..., bool], None]", getattr(fastapi.dependencies.utils, "_is_form_model_supplied", None)
)
# Before FastAPI 0.160.0, a form model doesn't take the form fields that other params of the tree declare as its extra
# fields. FastAPI collects their names with this function
_get_request_param_names = cast(
    "Union[Callable[[Dependant, Any], dict[str, set[str]]], None]",
    getattr(fastapi.dependencies.utils, "_get_request_param_names", None),
)
_should_embed_body_fields = cast(
    "Callable[[list[ModelField]], bool]", getattr(fastapi.dependencies.utils, "_should_embed_body_fields", None)
)
_get_multidict_value = cast(
    "Callable[[ModelField, FormData, str], Any]", getattr(fastapi.dependencies.utils, "_get_multidict_value", None)
)
# Yields the alias and the value that FastAPI picks from a form for each field, e.g. the bytes of a `bytes` upload
_extract_form_body = cast(
    "Callable[[list[ModelField], FormData], AsyncIterator[tuple[str, Any, Union[ModelField, None]]]]",
    getattr(fastapi.dependencies.utils, "_extract_form_body", None),
)
# Since FastAPI 0.153.0, FastAPI normalizes the dict body of a union of form models with this function for each model.
# It takes the fields of the model from the dict with `_extract_form_items`
_normalize_form_model = cast(
    "Callable[..., dict[str, Any]]", getattr(fastapi.dependencies.utils, "_normalize_form_model", None)
)
# Yields the alias that FastAPI picks from a form for each field and the other keys of the form, without reading uploads
_extract_form_items = cast(
    "Callable[[list[ModelField], FormData], Iterator[tuple[str, Any, Union[ModelField, None]]]]",
    getattr(fastapi.dependencies.utils, "_extract_form_items", None),
)


def _can_be_called_with(  # pragma: no cover # FastAPI >= 0.153
    function: Union[Callable[..., Any], None], *args: Any, **kwargs: Any
) -> bool:
    try:
        inspect.signature(cast("Callable[..., Any]", function)).bind(*args, **kwargs)
    except (TypeError, ValueError):
        return False
    return True


# We call these functions as FastAPI 0.153.0 to 0.162 do, so a change of their arguments must fail at import
if _normalize_form_model is not None and not (  # pragma: no cover
    _can_be_called_with(_normalize_form_model, None, model=None)
    and _can_be_called_with(_extract_form_items, None, None)
    and _can_be_called_with(_get_multidict_value, None, None, None)
    and _can_be_called_with(_get_cached_model_fields, None)
):
    raise ImportError(
        "Cadwyn does not support this version of FastAPI: `_normalize_form_model`, `_extract_form_items`, "
        "`_get_multidict_value`, or `get_cached_model_fields` is missing from `fastapi.dependencies.utils` or takes "
        "other arguments. Cadwyn uses them to pass unions of form models to FastAPI. Please, open an issue on "
        "Cadwyn's GitHub."
    )


def _get_form_models_before_fastapi_0_161(field: ModelField) -> list[type[BaseModel]]:  # pragma: no cover
    model = _get_form_model(field)
    return [] if model is None else [model]


# Since FastAPI 0.161.0, unions of form models are form models too, and `_get_form_model` returns None for them
_get_form_models = cast(
    "Callable[[ModelField], list[type[BaseModel]]]",
    getattr(fastapi.dependencies.utils, "_get_form_models", None)
    or (_get_form_models_before_fastapi_0_161 if _get_form_model is not None else None),
)


def _get_unsupported_fastapi_functions() -> list[str]:  # pragma: no cover # FastAPI >= 0.158.2
    """Return the private FastAPI functions that are missing or that Cadwyn can't call as it does."""
    # Before FastAPI 0.160.0, FastAPI doesn't take the form fields of other params as extra fields of form models
    needs_known_param_names = (
        "known_param_names" in inspect.signature(fastapi.dependencies.utils.request_body_to_args).parameters
    )
    has_form_model_unions = getattr(fastapi.dependencies.utils, "_get_form_models", None) is not None
    arg = object()
    calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any], bool]] = [
        # The name, the function, the args, the kwargs, and whether it must be an async generator function
        ("_get_form_models" if has_form_model_unions else "_get_form_model", _get_form_models, (arg,), {}, False),
        ("_get_param_aliases", _get_param_aliases, (arg,), {}, False),
        ("get_cached_model_fields", _get_cached_model_fields, (arg,), {}, False),
        ("_should_embed_body_fields", _should_embed_body_fields, (arg,), {}, False),
        ("_extract_form_body", _extract_form_body, (arg, arg), {}, True),
        ("_extract_form_items", _extract_form_items, (arg, arg), {}, False),
        ("_get_multidict_value", _get_multidict_value, (arg, arg, arg), {}, False),
    ]
    if not has_form_model_unions:
        calls.append(("_get_form_model", _get_form_model, (arg,), {}, False))
    if _is_form_model_supplied is not None:
        calls.append(("_is_form_model_supplied", _is_form_model_supplied, (arg, arg), {}, False))
        if needs_known_param_names:
            calls.append(("_is_form_model_supplied", _is_form_model_supplied, (arg, arg, arg), {}, False))
    if needs_known_param_names:
        calls.append(("_get_request_param_names", _get_request_param_names, (arg, arg), {}, False))
    if has_form_model_unions:
        calls.append(("_normalize_form_model", _normalize_form_model, (arg,), {"model": arg}, False))
    return [
        name
        for name, function, args, kwargs, is_async_generator in calls
        if function is None
        or not _can_be_called_with(function, *args, **kwargs)
        or inspect.isasyncgenfunction(function) is not is_async_generator
    ]


if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS and (  # pragma: no cover
    _unsupported_fastapi_functions := _get_unsupported_fastapi_functions()
):
    raise ImportError(
        "Cadwyn does not support this version of FastAPI: "
        f"{', '.join(f'`{name}`' for name in dict.fromkeys(_unsupported_fastapi_functions))} of "
        "`fastapi.dependencies.utils` is missing or takes other arguments. Cadwyn uses these functions to pass form "
        "models to FastAPI. Please, open an issue on Cadwyn's GitHub."
    )
if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS and not (  # pragma: no cover
    dataclasses.is_dataclass(Dependant)
    and all(isinstance(getattr(ModelField, name, None), property) for name in ("alias", "validation_alias"))
):
    raise ImportError(
        "Cadwyn does not support this version of FastAPI: `Dependant` is not a dataclass, or `ModelField` stores its "
        "aliases instead of taking them from its `field_info`. Cadwyn copies them with other aliases to pass form "
        "models to FastAPI. Please, open an issue on Cadwyn's GitHub."
    )
# The key that FastAPI looks up the dict body of the head endpoint's form model under if another form param has its
# alias, e.g. a form field that only an override declares. No form field has this key, so the param keeps its value
_FORM_MODEL_BODY_KEY = "\x00cadwyn_form_model_body"

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


def _dump_request_body(body: BaseModel) -> dict[str, Any]:
    """Return the data that the client has sent for a model body, without the values of its computed fields.

    The head would take them as input: as extra keys, as a 500 for models that forbid extra keys, or as the values of
    the fields that they share a key with. Like FastAPI, the head computes them itself. We can't tell whether an
    override of `model_dump()` takes `exclude_computed_fields` (e.g. the one of SQLModel before 0.0.26 doesn't), so we
    dump such models as before.
    """
    if type(body).model_dump is BaseModel.model_dump:
        return body.model_dump(by_alias=True, exclude_unset=True, exclude_computed_fields=True)
    return body.model_dump(by_alias=True, exclude_unset=True)


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
    # Embedded body fields get None only if `_get_body` has parsed a body without a content type as JSON null, which
    # it does only with `strict_content_type=False`.
    # FastAPI rejects a JSON null for them, so it has got these raw bytes instead, which it ignores like no body
    return embed_body_fields and not is_form


class _FormModelOmission(Enum):
    """Whether FastAPI has given the form model body param its default because the form doesn't supply it."""

    NOT_OMITTED = "not_omitted"
    # Every body param of the tree is an optional form model that the form doesn't supply
    OMITTED = "omitted"
    # Other body params of the tree, e.g. the form fields of a dependency override, can read the form
    OMITTED_NEXT_TO_OTHER_FORM_PARAMS = "omitted_next_to_other_form_params"


async def _get_form_model_omission(request: FastapiRequest, route: APIRoute) -> _FormModelOmission:
    """Return whether FastAPI has given the form model body param its default because the form doesn't supply it.

    Since FastAPI 0.158.2, FastAPI does so for an optional form model if the form has none of its fields. Cadwyn sees
    that default as None, and the head would reject None as a form body, so we omit the body for the head instead.
    """
    if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS:  # pragma: no cover # FastAPI >= 0.158.2
        return await _get_omission_of_optional_form_model(request, route)
    return _FormModelOmission.NOT_OMITTED


async def _get_omission_of_optional_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    request: FastapiRequest, route: APIRoute
) -> _FormModelOmission:
    """Return whether the body param is a form model that FastAPI has given its default, and what else reads the form.

    The body param is the only body param of the endpoint, whatever its name or alias. FastAPI only skips the form
    models (or unions of them) that can be None if the form doesn't supply any of them. Dependencies and their
    overrides can have such form models too.
    """
    body_param = route.dependant.body_params[0]
    if (
        not _get_form_models(body_param)
        or body_param.field_info.is_required()
        or type(None) not in get_args(body_param.field_info.annotation)
    ):
        return _FormModelOmission.NOT_OMITTED
    # FastAPI has already parsed the form, so this returns its cached form without reading it again or closing it
    form = await request.form()
    known_param_names = _get_known_form_param_names(route.dependant, route)
    if any(_form_supplies_form_model(model, form, known_param_names) for model in _get_form_models(body_param)):
        return _FormModelOmission.NOT_OMITTED
    for dependant in _iter_dependants_that_fastapi_solves(route.dependant, route):
        for field in dependant.body_params:
            field_models = _get_form_models(field)
            if (
                not field_models
                or type(None) not in get_args(field.field_info.annotation)
                or any(_form_supplies_form_model(model, form, known_param_names) for model in field_models)
            ):
                return _FormModelOmission.OMITTED_NEXT_TO_OTHER_FORM_PARAMS
    return _FormModelOmission.OMITTED


async def _get_form_for_omitted_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    request: FastapiRequest, head_dependant: Dependant, head_route: APIRoute
) -> FormData:
    """Return the form for the head when its form model is omitted, but other params of the tree can read the form.

    No migration has set the body, so the head gives the form model its default, and every other param the value that
    FastAPI gives it from the form. That is only so if the form doesn't supply the form model in the head either.
    """
    # FastAPI has already parsed the form, so this returns its cached form without reading it again or closing it
    form = await request.form()
    # We pass the form to the head as it is, so we close its uploads after the request like those of other forms
    _get_uploads_to_close(request).add(form.multi_items())
    body_param = head_dependant.body_params[0]
    known_param_names = _get_known_form_param_names(head_dependant, head_route)
    if any(_form_supplies_form_model(model, form, known_param_names) for model in _get_form_models(body_param)):
        raise CadwynError(
            f"Cadwyn can't pass the form of this request to the latest version: the request has none of the fields of "
            f"the form model `{body_param.name}` in its version, so the form model gets its default, but the form has "
            "fields of the form model in the latest version, and other params of the endpoint, of its dependencies, "
            "or of their overrides read the form as well. Give these params other names or aliases than the fields "
            "of the form model."
        )
    return form


def _form_supplies_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    model: type[BaseModel], form: FormData, known_param_names: Union[set[str], None] = None
) -> bool:
    """Return whether FastAPI validates the optional form model from the form instead of giving it its default.

    Before FastAPI 0.160.0, FastAPI doesn't take the keys of `known_param_names` as extra fields of the form model.
    """
    if _is_form_model_supplied is not None:
        if known_param_names is None:
            return _is_form_model_supplied(model, form)
        return _is_form_model_supplied(model, form, known_param_names)
    # Before FastAPI 0.159.1, FastAPI checks the same inline
    own_names = {alias for field in _get_cached_model_fields(model) for alias, _ in _get_param_aliases(field)}
    return bool(
        own_names.intersection(form)
        or (model.model_config.get("extra") in ("allow", "forbid") and form.keys() - (known_param_names or own_names))
    )


def _get_known_form_param_names(  # pragma: no cover # FastAPI >= 0.158.2
    dependant: Dependant, route: APIRoute
) -> Union[set[str], None]:
    """Return the names of the form params that FastAPI doesn't take as extra fields of form models, if it has them.

    Before FastAPI 0.160.0, FastAPI collects them from the tree that it solves, with the overrides of dependencies.
    """
    if _get_request_param_names is None:
        return None
    return _get_request_param_names(dependant, route.dependency_overrides_provider)["form"]


def _body_is_in_kwargs(route: APIRoute, body_field_alias: str, *, embed_body_fields: bool) -> bool:
    """Return whether we take the body from the kwarg of the body param instead of reading it from the request.

    When the body is embedded (wrapped into a separate field with the "body" alias, e.g. for files and forms), no
    kwarg holds the whole body: a kwarg named "body" is a param of its own (a file, a query param, a dependency, etc.),
    so we read the body from the request. Otherwise, the kwarg of the body param holds the body. We take it from
    there if the body's alias is the body param's name or the name of a dependency, and read the request otherwise.
    """
    if embed_body_fields:
        return False
    return body_field_alias == route.dependant.body_params[0].name or any(
        dependency.name == body_field_alias for dependency in route.dependant.dependencies
    )


class _FormModelLookup(NamedTuple):
    """The dependant to solve for the head, and how to pass the dict body of its form model to FastAPI."""

    dependant: Dependant
    # The keys to put the dict body of the form model under
    aliases: tuple[str, ...] = ()
    # The form models that read the dict body of the head's union of form models, if FastAPI validates the models of
    # the union from it as if it were form data
    form_union_readers: tuple["_FormModelReader", ...] = ()
    # Whether the union is a form model, as it is since FastAPI 0.161.0
    form_union_is_form_model: bool = False
    # The values that FastAPI gives other params for the migrated request, e.g. the form fields that only a dependency
    # override declares, under the keys that these params look them up under in the dict body
    form_values: Mapping[str, Any] = MappingProxyType({})
    # Whether the other keys of the body are next to the form model, as in the last release
    spreads_body: bool = True


class _FormModelReader(NamedTuple):
    """A form model that reads the dict body of a union of form models."""

    model: type[BaseModel]
    # Whether FastAPI validates the model from the dict as if it were form data, as it does for a model of a union
    is_validated_as_form_data: bool


async def _get_form_model_lookup(
    request: FastapiRequest,
    head_dependant: Dependant,
    head_route: APIRoute,
    body: Any,
    dumped_body: Union[BaseModel, None],
    *,
    embed_body_fields: bool,
    is_migrated: bool,
) -> _FormModelLookup:
    """Return the dependant to solve for the head, and how to pass the dict body of its form model to FastAPI.

    Before FastAPI 0.158.2, FastAPI validates the dict body of a single form model as it is. Since then, the endpoint
    and every dependency that shares the form model look it up under their own aliases instead, and the other body
    params, e.g. the form fields and form models that only a dependency override declares, look their values up in the
    same dict.

    Every such param must get what FastAPI would give it if the client had sent the migrated request, so we work out
    that value for each of them like FastAPI does (see `_get_virtual_form`). We pass the body as the last release did
    if that gives every param exactly its value. Otherwise, we pass these values under their keys, and solve a copy of
    the head's dependant whose form model looks the body up under a key that no form param has. If neither does, or we
    can't work out the value of a param, e.g. of a param that takes the whole body, we fail instead of giving a param
    another value.
    """
    body_field = head_dependant.body_params[0]
    form_models = _get_form_models(body_field) if _FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS else []
    form_union_readers = _get_form_union_readers(
        head_dependant, head_route, is_form_model=bool(form_models), embed_body_fields=embed_body_fields
    )
    if form_models:  # pragma: no cover # FastAPI >= 0.158.2
        params = _get_body_lookups(head_dependant, head_route, set(form_models), embed_body_fields=embed_body_fields)
        if dumped_body is None and (
            type(body) not in (dict, OrderedDict, defaultdict)
            or not any(
                _form_supplies_form_model(
                    model, FormData(body), _get_known_form_param_names(head_dependant, head_route)
                )
                for model in form_models
            )
        ):
            raise _cant_pass_form_error(
                params[0],
                "the migration must supply a dict with fields of the omitted form model while other params read "
                "the form",
            )
        lookup = _FormModelLookup(
            head_dependant,
            _get_form_model_aliases(params, own_key=False),
            form_union_readers,
            form_union_is_form_model=True,
        )
        if all(param.kind is _BodyParamKind.SAME_FORM_MODEL for param in params):
            return lookup
        virtual_form = await _get_virtual_form(request, body, dumped_body, is_migrated=is_migrated)
        expected_values = await _get_expected_values_of_body_params(
            params, virtual_form, _get_known_form_param_names(head_dependant, head_route)
        )
        for index, param in enumerate(params):
            if expected_values.get(index) is _FormValueMarker.UNKNOWN:
                raise _cant_pass_form_error(param, _describe_unknown_value(param))
        if _get_param_with_other_value(lookup, params, expected_values, body, own_key=False) is None:
            return lookup
        form_values: dict[str, Any] = {}
        for expected_value in expected_values.values():
            if isinstance(expected_value, _ExpectedFormValue):
                form_values.setdefault(expected_value.alias, expected_value.value)
        lookup = _FormModelLookup(
            _with_form_models_under_own_key(head_dependant),
            _get_form_model_aliases(params, own_key=True),
            form_union_readers,
            form_union_is_form_model=True,
            form_values=form_values,
            spreads_body=False,
        )
        mismatching_param = _get_param_with_other_value(lookup, params, expected_values, body, own_key=True)
        if mismatching_param is None:
            return lookup
        raise _cant_pass_form_error(
            mismatching_param,
            "it has the same key as a form model or another param. Give them other names or aliases",
        )
    return _FormModelLookup(head_dependant, form_union_readers=form_union_readers)


def _get_form_union_readers(
    head_dependant: Dependant, head_route: APIRoute, *, is_form_model: bool, embed_body_fields: bool
) -> tuple[_FormModelReader, ...]:
    """Return the form models that read the dict body of the head's union of form models.

    We return them only if FastAPI validates the models of the union from the dict as if it were form data. Before
    FastAPI 0.161.0, the union isn't a form model, so only a body param that isn't embedded gets the whole
    body. Since then, every form model of the tree that FastAPI solves reads the body under its param's alias.
    """
    body_field = head_dependant.body_params[0]
    if not _get_models_validated_as_form_data(body_field):
        return ()
    if is_form_model:  # pragma: no cover # FastAPI >= 0.161
        fields = [
            field
            for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
            for field in dependant.body_params
            if _get_form_models(field)
        ]
    elif embed_body_fields:  # pragma: no cover # FastAPI >= 0.153
        return ()
    else:  # pragma: no cover # FastAPI >= 0.153
        fields = [body_field]
    readers: dict[_FormModelReader, None] = {}  # pragma: no cover # FastAPI >= 0.153
    for field in fields:  # pragma: no cover # FastAPI >= 0.153
        models = _get_models_validated_as_form_data(field)
        for model in models or _get_form_models(field):
            readers[_FormModelReader(model, is_validated_as_form_data=bool(models))] = None
    return tuple(readers)  # pragma: no cover # FastAPI >= 0.153


def _get_models_validated_as_form_data(field: ModelField) -> list[type[BaseModel]]:
    """Return the models of a union of form models that FastAPI validates from a dict as if it were form data.

    Since FastAPI 0.153.0, FastAPI adds a validator that does this to each of them.
    """
    if _normalize_form_model is None:
        return []
    return [  # pragma: no cover # FastAPI >= 0.153
        validator.func.keywords["model"]
        for arg in get_args(field.field_info.annotation)
        if get_origin(arg) is Annotated
        for validator in arg.__metadata__
        if isinstance(validator, BeforeValidator)
        and isinstance(validator.func, functools.partial)
        and validator.func.func is _normalize_form_model
        and lenient_issubclass(validator.func.keywords.get("model"), BaseModel)
    ]


def _cant_pass_form_error(  # pragma: no cover # FastAPI >= 0.158.2
    param: "_BodyParamLookup", reason: str
) -> CadwynError:
    return CadwynError(
        "Cadwyn can't pass the form of this request to the latest version: FastAPI looks the form models and the "
        f"other form params up in the same dict body, so the param {_describe_body_param(param)} would get another "
        f"value than the one that FastAPI gives it for the migrated request, because {reason}."
    )


def _describe_unknown_value(param: "_BodyParamLookup") -> str:  # pragma: no cover # FastAPI >= 0.158.2
    if param.kind is _BodyParamKind.WHOLE_BODY:
        return (
            "it takes the whole body, which has the form model's data under the key of the form model. Read the form "
            "from the `Request` in that dependency instead"
        )
    return (
        "it reads a key of the form that the form model has taken, and Cadwyn can't tell its value after the request "
        "migrations, e.g. because the body of the form model isn't a dict, the form model has taken the key under "
        "another name (e.g. through `AliasChoices`), or a migration has put a collection (e.g. a list, a set, or a "
        "tuple) into the body that a union of form models gets. Give the param another name or alias"
    )


def _get_omitted_form_model_lookup(  # pragma: no cover # FastAPI >= 0.158.2
    head_dependant: Dependant, head_route: APIRoute, body: Any, *, embed_body_fields: bool
) -> _FormModelLookup:
    """Return how to pass the body that a migration has set for an omitted optional form model to the head.

    We put a dict body under the aliases of the params that have the same form models as the endpoint's body param,
    e.g. those of the dependencies and overrides that share it, so that other form models still get their defaults.
    We look at the tree that FastAPI solves for the head, which can have dependencies that the request's version lacks.
    In every other case we pass the body as it is, like the last release did: if the body isn't a dict, an
    `OrderedDict`, or a `defaultdict`, if it has a key with such an alias (e.g. a migration has put the form model's
    data under it), if another param of the tree has such an alias or gets the whole body, if, like a form, it supplies
    none of the form models, so that the param gets its default as FastAPI gives it, if a form model that allows or
    forbids extra fields would take a key of another param as an extra field, which FastAPI doesn't do for a form
    before 0.160.0, or if FastAPI would validate a model of a union from changed values.
    """
    # We only know how the lookups of these dicts behave, while a subclass can override them
    if type(body) not in (dict, OrderedDict, defaultdict):
        return _FormModelLookup(head_dependant)
    form_models = set(_get_form_models(head_dependant.body_params[0]))
    aliases: dict[str, None] = {}
    other_aliases: set[str] = set()
    # The keys that the other params of the tree read, including the fields of their form models
    other_keys: set[str] = set()
    for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route):
        # Like FastAPI, we give a single body param that isn't embedded the whole body
        is_embedded = embed_body_fields or _should_embed_body_fields(dependant.body_params)
        for field in dependant.body_params:
            field_form_models = set(_get_form_models(field))
            if not field_form_models and not is_embedded:
                return _FormModelLookup(head_dependant)
            field_aliases = [alias for alias, _ in _get_param_aliases(field)]
            if field_form_models == form_models:
                aliases.update(dict.fromkeys(field_aliases))
            else:
                other_aliases.update(field_aliases)
                other_keys.update(field_aliases, _get_field_aliases_of_models(field_form_models))
    if (
        not aliases.keys().isdisjoint(body)
        or not aliases.keys().isdisjoint(other_aliases)
        or not any(_form_supplies_form_model(model, FormData(body)) for model in form_models)
        or (
            any(model.model_config.get("extra") in ("allow", "forbid") for model in form_models)
            and not (other_keys - _get_field_aliases_of_models(form_models)).isdisjoint(body)
        )
    ):
        return _FormModelLookup(head_dependant)
    lookup = _FormModelLookup(head_dependant, tuple(aliases))
    if len(form_models) > 1:
        readers = tuple(_FormModelReader(model, is_validated_as_form_data=True) for model in form_models)
        lookup = lookup._replace(form_union_readers=readers, form_union_is_form_model=True)
        try:
            form_model_body = _get_form_union_body(body, readers)
        except _FormUnionValueError:
            return _FormModelLookup(head_dependant)
        if _form_model_union_changes_body(form_model_body, form_models):
            return _FormModelLookup(head_dependant)
    return lookup


def _get_field_aliases_of_models(  # pragma: no cover # FastAPI >= 0.158.2
    models: set[type[BaseModel]],
) -> set[str]:
    return {
        alias for model in models for field in _get_cached_model_fields(model) for alias, _ in _get_param_aliases(field)
    }


def _form_model_union_changes_body(  # pragma: no cover # FastAPI >= 0.161
    body: dict[str, Any], form_models: set[type[BaseModel]]
) -> bool:
    """Return whether FastAPI would validate a model of the union from another dict than the body.

    FastAPI validates each model of the union from the body as if it were form data, e.g. a field that takes a single
    value gets one value of a list, any other key gets the only value of a list as it is, and a field with an empty
    string gets its default. A model can read any key, e.g. through an `AliasPath`, a root model, or a validator,
    so we compare the whole dict.
    """
    return any(_normalize_form_model(body, model=model) != body for model in form_models)


def _with_form_models_under_own_key(dependant: Dependant) -> Dependant:  # pragma: no cover # FastAPI >= 0.158.2
    """Return a copy of the dependant whose own form models FastAPI looks up under `_FORM_MODEL_BODY_KEY`.

    If the app has dependency overrides, FastAPI builds the dependants of all dependencies again while solving, so the
    form models of dependencies keep their aliases.
    """
    return dataclasses.replace(
        dependant,
        body_params=[
            _with_form_model_under_own_key(field) if _get_form_models(field) else field
            for field in dependant.body_params
        ],
    )


def _with_form_model_under_own_key(field: ModelField) -> ModelField:  # pragma: no cover # FastAPI >= 0.158.2
    # The field keeps its type adapter, so it validates the body exactly like the original
    field_info = copy.copy(field.field_info)
    field_info.alias = _FORM_MODEL_BODY_KEY
    field_info.validation_alias = None
    field = copy.copy(field)
    field.field_info = field_info
    return field


class _BodyParamKind(Enum):  # pragma: no cover # FastAPI >= 0.158.2
    # A form model param with the same form models as the endpoint's body param, which gets the body
    SAME_FORM_MODEL = "same_form_model"
    # A form model param with other form models
    OTHER_FORM_MODEL = "other_form_model"
    # A param that FastAPI looks up in the body by its aliases, e.g. a form field or `Body(embed=True)`
    FORM_FIELD = "form_field"
    # A param that gets the whole body, e.g. the only `Body()` param of a dependency override
    WHOLE_BODY = "whole_body"


class _BodyParamLookup(NamedTuple):  # pragma: no cover # FastAPI >= 0.158.2
    """A body param of the tree that FastAPI solves for the head, and the aliases that FastAPI looks it up under."""

    field: ModelField
    aliases: list[str]
    kind: _BodyParamKind
    # The endpoint, dependency, or override that declares the param
    call: Any
    # Whether the head's dependant declares it itself, so that a copy of the dependant can change its alias
    is_own: bool


def _get_body_lookups(  # pragma: no cover # FastAPI >= 0.158.2
    head_dependant: Dependant, head_route: APIRoute, form_models: set[type[BaseModel]], *, embed_body_fields: bool
) -> list[_BodyParamLookup]:
    """Return how the tree that FastAPI solves for the head reads a dict body."""
    lookups: list[_BodyParamLookup] = []
    for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route):
        # Like FastAPI, we give a single body param that isn't embedded the whole body
        is_embedded = embed_body_fields or _should_embed_body_fields(dependant.body_params)
        for field in dependant.body_params:
            field_form_models = _get_form_models(field)
            if field_form_models:
                kind = (
                    _BodyParamKind.SAME_FORM_MODEL
                    if set(field_form_models) == form_models
                    else _BodyParamKind.OTHER_FORM_MODEL
                )
            else:
                kind = _BodyParamKind.FORM_FIELD if is_embedded else _BodyParamKind.WHOLE_BODY
            aliases = [alias for alias, _ in _get_param_aliases(field)]
            lookups.append(_BodyParamLookup(field, aliases, kind, dependant.call, dependant is head_dependant))
    return lookups


def _get_lookup_aliases(
    param: _BodyParamLookup, *, own_key: bool
) -> list[str]:  # pragma: no cover # FastAPI >= 0.158.2
    """Return the aliases that FastAPI looks the param up under, given whether the own form models use their own key."""
    if own_key and param.is_own and param.kind in (_BodyParamKind.SAME_FORM_MODEL, _BodyParamKind.OTHER_FORM_MODEL):
        return [_FORM_MODEL_BODY_KEY]
    return param.aliases


def _get_form_model_aliases(  # pragma: no cover # FastAPI >= 0.158.2
    params: list[_BodyParamLookup], *, own_key: bool
) -> tuple[str, ...]:
    """Return the keys to put the body under.

    The last release put it under the aliases of every form model. In the copy of the dependant, only the form models
    that share the endpoint's form models get it, and the other ones get their own values.
    """
    kinds = (
        (_BodyParamKind.SAME_FORM_MODEL,)
        if own_key
        else (_BodyParamKind.SAME_FORM_MODEL, _BodyParamKind.OTHER_FORM_MODEL)
    )
    return tuple(
        dict.fromkeys(
            alias for param in params if param.kind in kinds for alias in _get_lookup_aliases(param, own_key=own_key)
        )
    )


class _ExpectedFormValue(NamedTuple):  # pragma: no cover # FastAPI >= 0.158.2
    """The value that a param gets in the head, and the key that it gets it under."""

    alias: str
    value: Any
    # Whether the value is FastAPI's from the form, so it is a string, an upload, bytes, False, or a list of them
    is_from_form: bool


class _FormValueMarker(Enum):  # pragma: no cover # FastAPI >= 0.158.2
    # The param gets its default because FastAPI finds none of its aliases
    DEFAULT = "default"
    # The migrated request doesn't have the key, e.g. because a migration has removed it from the form model's body
    ABSENT = "absent"
    # We can't know the value of the key in the migrated request, or the value of the param
    UNKNOWN = "unknown"


class _VirtualForm(NamedTuple):  # pragma: no cover # FastAPI >= 0.158.2
    """The form that the client would have sent as the migrated request."""

    # The keys whose values are the client's, which FastAPI reads from the form
    form: FormData
    # The values of the other keys: those of the migrated body, or markers
    overlay: dict[str, Union[_ExpectedFormValue, _FormValueMarker]]
    # Every key, in the order of the form, with the keys that only the migrated body has after them
    keys: list[str]


async def _get_virtual_form(  # pragma: no cover # FastAPI >= 0.158.2
    request: FastapiRequest, body: Any, dumped_body: Union[BaseModel, None], *, is_migrated: bool
) -> _VirtualForm:
    """Return the form that the client would have sent as the migrated request.

    If no request migration has run, it is the request's form. Otherwise, every key of the migrated body has its value
    from the body, and so do the keys that the request's form model has taken from the form: FastAPI's pick of each of
    its fields (e.g. the first alias of `AliasChoices` that the form has) and its extra fields. An omitted form model
    has taken no keys, so every original form key that the migration leaves out keeps its value. A taken key that the
    body no longer has is absent if the dump of the form model had it, and unknown otherwise, because the form model
    has dumped its value under another key. Every other key keeps the value of the form.
    """
    # FastAPI has already parsed the form, so this returns its cached form without reading it again or closing it
    form = await request.form()
    form_keys = list(dict.fromkeys(form.keys()))
    if not is_migrated:
        return _VirtualForm(form, {}, form_keys)
    taken_keys: set[str] = set()
    if dumped_body is not None:
        taken_keys.update(
            alias
            for alias, _, field in _extract_form_items(_get_cached_model_fields(type(dumped_body)), form)
            if field is not None
        )
        taken_keys.update(dumped_body.__pydantic_extra__ or {})
    overlay: dict[str, Union[_ExpectedFormValue, _FormValueMarker]] = {}
    if isinstance(body, dict):
        # `_embed_form_model` puts the same keys of the body next to the form model
        overlay = {key: _ExpectedFormValue(key, value, is_from_form=False) for key, value in {**body}.items()}
    # The same dump as the body's, so that we know which keys the client's version of the body had
    dump = _dump_request_body(dumped_body) if dumped_body is not None else {}
    for key in taken_keys:
        if key not in overlay:
            overlay[key] = (
                _FormValueMarker.ABSENT
                if isinstance(body, dict) and isinstance(dump, dict) and key in dump
                else _FormValueMarker.UNKNOWN
            )
    return _VirtualForm(
        FormData([(key, value) for key, value in form.multi_items() if key not in overlay]),
        overlay,
        [*form_keys, *(key for key in overlay if key not in form)],
    )


async def _get_expected_values_of_body_params(  # pragma: no cover # FastAPI >= 0.158.2
    params: list[_BodyParamLookup], virtual_form: _VirtualForm, known_param_names: Union[set[str], None]
) -> dict[int, Union[_ExpectedFormValue, _FormValueMarker]]:
    """Return what FastAPI would give each param other than the endpoint's form models for the migrated request.

    FastAPI solves a cached dependency once, so we work out its values once too, which reads its uploads once.
    """
    expected_values: dict[int, Union[_ExpectedFormValue, _FormValueMarker]] = {}
    picked_values: dict[tuple[int, str], Union[_ExpectedFormValue, _FormValueMarker]] = {}
    for index, param in enumerate(params):
        if param.kind is _BodyParamKind.SAME_FORM_MODEL:
            continue
        key = (id(param.call), param.field.name)
        if key not in picked_values:
            if param.kind is _BodyParamKind.FORM_FIELD:
                picked_values[key] = await _pick_form_value(param.field, param.aliases, virtual_form)
            elif param.kind is _BodyParamKind.OTHER_FORM_MODEL:
                picked_values[key] = await _get_expected_form_model_value(param, virtual_form, known_param_names)
            else:
                # A param that takes the whole body would see the form model's data under the form model's key
                picked_values[key] = _FormValueMarker.UNKNOWN
        expected_values[index] = picked_values[key]
    return expected_values


async def _pick_form_value(  # pragma: no cover # FastAPI >= 0.158.2
    field: ModelField, aliases: list[str], virtual_form: _VirtualForm
) -> Union[_ExpectedFormValue, _FormValueMarker]:
    """Return the alias and the value that FastAPI would pick for the field from the virtual form, if any.

    Like FastAPI, we take the first alias that has a value, and read only that value, e.g. not an upload under a later
    alias, which a dependency may have closed. FastAPI reads the keys of the form with its own function.
    """
    overlay = virtual_form.overlay
    # FastAPI gives a missing boolean form field False before 0.162.0, but only if the form has none of its aliases
    has_any_alias = any(
        alias in virtual_form.form or overlay.get(alias, _FormValueMarker.ABSENT) is not _FormValueMarker.ABSENT
        for alias in aliases
    )
    # The aliases after the last one of the overlay, which FastAPI looks up in the form
    form_aliases: list[str] = []
    for alias in [*aliases, None]:
        if alias is not None and alias not in overlay:
            form_aliases.append(alias)
            continue
        if form_aliases and (not has_any_alias or not virtual_form.form.keys().isdisjoint(form_aliases)):
            form = FormData([(key, value) for key, value in virtual_form.form.multi_items() if key in form_aliases])
            form_value = await _extract_form_value(field, form)
            if form_value is not None:
                return form_value
        if alias is None:
            return _FormValueMarker.DEFAULT
        if overlay[alias] is not _FormValueMarker.ABSENT:
            return overlay[alias]
        form_aliases = []
    return _FormValueMarker.DEFAULT


async def _get_expected_form_model_value(  # pragma: no cover # FastAPI >= 0.158.2
    param: _BodyParamLookup, virtual_form: _VirtualForm, known_param_names: Union[set[str], None]
) -> Union[_ExpectedFormValue, _FormValueMarker]:
    """Return the dict that FastAPI would validate the form model param from for the migrated request.

    Like FastAPI, an optional form model that the form doesn't supply gets its default. A single form model gets the
    pick of each of its fields and the other keys of the form, e.g. for its extra fields or validators. Before FastAPI
    0.160.0, the other keys don't include the form fields of other params. A union of form models gets every key.
    """
    if _FormValueMarker.UNKNOWN in virtual_form.overlay.values():
        return _FormValueMarker.UNKNOWN
    keys = [key for key in virtual_form.keys if virtual_form.overlay.get(key) is not _FormValueMarker.ABSENT]
    models = _get_form_models(param.field)
    if type(None) in get_args(param.field.field_info.annotation) and not any(
        _form_supplies_form_model(model, FormData([(key, "") for key in keys]), known_param_names) for model in models
    ):
        return _FormValueMarker.DEFAULT
    value: dict[str, Any] = {}
    field_aliases: set[str] = set()
    own_names: set[str] = set()
    if len(models) == 1:
        for model_field in _get_cached_model_fields(models[0]):
            aliases = [alias for alias, _ in _get_param_aliases(model_field)]
            own_names.update(aliases)
            picked_value = await _pick_form_value(model_field, aliases, virtual_form)
            # FastAPI visits aliases only through the first one with a value. Later aliases remain available
            # as extra fields, even when another param also declares them.
            for alias in aliases:
                field_aliases.add(alias)
                if isinstance(picked_value, _ExpectedFormValue) and alias == picked_value.alias:
                    break
            if isinstance(picked_value, _ExpectedFormValue):
                value[picked_value.alias] = picked_value.value
            elif picked_value is _FormValueMarker.UNKNOWN:
                return picked_value
    for key in keys:
        if key in field_aliases or (
            len(models) == 1 and known_param_names is not None and key in known_param_names and key not in own_names
        ):
            continue
        overlay_value = virtual_form.overlay.get(key)
        if isinstance(overlay_value, _ExpectedFormValue):
            # FastAPI would validate the models of the union from a collection of the body as if it were repeated form
            # values, so a model could get only part of it
            if len(models) > 1 and isinstance(overlay_value.value, _FORM_VALUE_COLLECTIONS):
                return _FormValueMarker.UNKNOWN
            value[key] = overlay_value.value
        else:
            items = virtual_form.form.getlist(key)
            value[key] = items[0] if len(items) == 1 else items
    return _ExpectedFormValue(param.aliases[0], value, is_from_form=False)


async def _extract_form_value(  # pragma: no cover # FastAPI >= 0.158.2
    field: ModelField, form: FormData
) -> Union[_ExpectedFormValue, None]:
    """Return the alias and the value that FastAPI picks for the field from the form, if any."""
    # FastAPI yields the other keys of the form too, so we go through all of them to close the generator
    values = [
        _ExpectedFormValue(alias, value, is_from_form=True)
        async for alias, value, item_field in _extract_form_body([field], form)
        if item_field is not None
    ]
    return values[0] if values else None


def _get_param_with_other_value(  # pragma: no cover # FastAPI >= 0.158.2
    lookup: _FormModelLookup,
    params: list[_BodyParamLookup],
    expected_values: dict[int, Union[_ExpectedFormValue, _FormValueMarker]],
    body: Any,
    *,
    own_key: bool,
) -> Union[_BodyParamLookup, None]:
    """Return a param that FastAPI would give another value than the expected one from the embedded body, if any.

    Like FastAPI, we take the value of the first alias of the param that the body has. Every param has an expected
    value: the endpoint's form models get the body, and the other params their values for the migrated request.
    """
    embedded_body = _embed_form_model(body, lookup)
    form_model_body = embedded_body[lookup.aliases[0]]
    for index, param in enumerate(params):
        found_alias = next(
            (alias for alias in _get_lookup_aliases(param, own_key=own_key) if alias in embedded_body), None
        )
        expected = expected_values.get(index)
        if param.kind is _BodyParamKind.SAME_FORM_MODEL:
            is_expected = found_alias is not None and embedded_body[found_alias] is form_model_body
        elif not isinstance(expected, _ExpectedFormValue):
            is_expected = expected is _FormValueMarker.DEFAULT and found_alias is None
        elif found_alias != expected.alias:
            is_expected = False
        else:
            found_value = embedded_body[found_alias]
            is_expected = (
                found_value is expected.value
                # FastAPI's values from the form can't run user code when compared, unlike the values of the body
                or (
                    expected.is_from_form
                    and found_alias in lookup.form_values
                    and found_alias not in lookup.aliases
                    and found_value == expected.value
                )
                or (param.kind is _BodyParamKind.OTHER_FORM_MODEL and _is_same_dict(found_value, expected.value))
            )
        if not is_expected:
            return param
    return None


def _is_same_dict(found: Any, expected: dict[str, Any]) -> bool:  # pragma: no cover # FastAPI >= 0.158.2
    """Return whether the dict has the same keys in the same order, and the very same values."""
    return (
        type(found) is dict
        and list(found) == list(expected)
        and all(found[key] is value for key, value in expected.items())
    )


def _describe_body_param(param: _BodyParamLookup) -> str:  # pragma: no cover # FastAPI >= 0.158.2
    call_name = getattr(param.call, "__qualname__", None) or repr(param.call)
    return f"`{param.field.name}` of `{call_name}`"


def _iter_dependants_that_fastapi_solves(  # pragma: no cover # FastAPI >= 0.158.2
    dependant: Dependant, route: APIRoute
) -> Iterator[Dependant]:
    """Yield the dependant and every sub-dependant in the tree that FastAPI solves for the route.

    Like FastAPI, we solve the dependant of an override instead of the overridden dependency.
    """
    dependency_overrides = _get_dependency_overrides(route)
    dependants = [dependant]
    while dependants:
        dependant = dependants.pop()
        yield dependant
        for sub_dependant in dependant.dependencies:
            if dependency_overrides and (call := dependency_overrides.get(sub_dependant.call)) is not None:
                sub_dependant = get_dependant(
                    path=cast("str", sub_dependant.path), call=call, name=sub_dependant.name, scope=sub_dependant.scope
                )
            dependants.append(sub_dependant)


def _embed_form_model(body: Any, form_model_lookup: _FormModelLookup) -> Any:
    """Return the body with the form models embedded under their aliases.

    Other form fields, e.g. the ones that only a dependency override declares, look their values up in the body itself,
    as they did before FastAPI 0.158.2, or in the values that FastAPI gives them from the form. A form model of an
    override wins over a form field with the same alias.

    FastAPI takes the dict body of a union of form models as form data, so we pass the union a copy that FastAPI turns
    back into the body's values. If there's no such copy, then since FastAPI 0.161.0, we raise, because the last
    release failed for this body too. Before FastAPI 0.161.0, the last release passed the body as it is, and FastAPI
    gave one of the models only part of a value. That works for some apps, e.g. if that model rejects the body anyway
    because of another field, so we keep it.
    """
    form_model_body = body
    if form_model_lookup.form_union_readers and isinstance(body, dict):  # pragma: no cover # FastAPI >= 0.153
        try:
            form_model_body = _get_form_union_body(body, form_model_lookup.form_union_readers)
        except _FormUnionValueError:
            if form_model_lookup.form_union_is_form_model:
                raise
            return body
    if not form_model_lookup.aliases:
        return form_model_body
    return {  # pragma: no cover # FastAPI >= 0.158.2
        **(body if isinstance(body, dict) and form_model_lookup.spreads_body else {}),
        **form_model_lookup.form_values,
        **dict.fromkeys(form_model_lookup.aliases, form_model_body),
    }


class _FormUnionValueError(CadwynError):
    pass


_FORM_VALUE_COLLECTIONS = (list, set, frozenset, tuple, deque)


def _get_form_union_body(  # pragma: no cover # FastAPI >= 0.153
    body: dict[str, Any], readers: tuple[_FormModelReader, ...]
) -> dict[str, Any]:
    """Return a copy of the body that FastAPI turns back into the body's values for every form model that reads it.

    FastAPI validates every model of a union of form models from the dict body as if it were form data: it takes a
    list as repeated form values, so a field that takes a single value gets only the last of them, and any other value
    as a single form value, so a field that takes repeated values gets the whole set or tuple as one of them. So for
    every list, set, frozenset, tuple, or deque, we pass its items as repeated form values, or the whole value as a
    single form value (a list in a list). Form models outside the union validate the dict as it is.

    We start with what the fields of each key take, and check the whole copy with FastAPI's own function, because the
    keys affect each other: a field takes the first of its aliases that has a value, and FastAPI takes the others as
    extra fields. While some keys come out wrong, we switch a single key, first a wrong one, then another alias of a
    field of a wrong one, and keep the switch if fewer keys come out wrong and no other one does. If no switch helps,
    a model would silently get a wrong value, e.g. because it takes a single value for the key and another one takes
    repeated values, so we raise.
    """
    fields_of_readers = {reader: _get_form_model_fields(reader.model) for reader in readers}
    readers_of_keys = {
        key: [reader for reader in readers if fields_of_readers[reader].reads(key)]
        for key, value in body.items()
        if isinstance(value, _FORM_VALUE_COLLECTIONS)
    }
    # Only the models of the union get the values as form data, so the other values reach every model as they are
    keys = [
        key
        for key, key_readers in readers_of_keys.items()
        if any(reader.is_validated_as_form_data for reader in key_readers)
    ]
    if not keys:
        return body
    checked_readers = {reader for key in keys for reader in readers_of_keys[key]}

    def get_wrong_keys(single_value_keys: frozenset[str]) -> tuple[dict[str, Any], set[str]]:
        union_body = {
            key: _as_form_value(value, as_single_value=key in single_value_keys) if key in keys else value
            for key, value in body.items()
        }
        bodies_of_readers = {
            reader: _normalize_form_model(union_body, model=reader.model)
            if reader.is_validated_as_form_data
            else union_body
            for reader in checked_readers
        }
        wrong_keys = {
            key
            for key in keys
            if not all(
                _form_model_gets_value(fields_of_readers[reader], key, body[key], bodies_of_readers[reader])
                for reader in readers_of_keys[key]
            )
        }
        return union_body, wrong_keys

    single_value_keys = frozenset(
        key for key in keys if _takes_single_form_value(body[key], readers_of_keys[key], fields_of_readers, key)
    )
    union_body, wrong_keys = get_wrong_keys(single_value_keys)
    # Each switch that we keep fixes a key, so this is at most quadratic in the number of keys
    switches_left = 2 * len(keys)
    while wrong_keys:
        switched = False
        for key in _get_keys_to_switch(wrong_keys, keys, readers_of_keys, fields_of_readers):
            if switches_left == 0:
                break
            switches_left -= 1
            candidate_keys = single_value_keys ^ {key}
            candidate_body, candidate_wrong_keys = get_wrong_keys(candidate_keys)
            if candidate_wrong_keys < wrong_keys:
                single_value_keys, union_body, wrong_keys = candidate_keys, candidate_body, candidate_wrong_keys
                switched = True
                break
        if not switched:
            key = next(key for key in keys if key in wrong_keys)
            raise _FormUnionValueError(_describe_form_union_value_error(key, body[key], readers_of_keys[key]))
    return union_body


def _takes_single_form_value(  # pragma: no cover # FastAPI >= 0.153
    value: Any,
    readers: list[_FormModelReader],
    fields_of_readers: "dict[_FormModelReader, _FormModelFields]",
    key: str,
) -> bool:
    """Return whether to start with the value as a single form value.

    That's the case if no field with the key takes repeated values, and no model outside the union would get the list
    in a list.
    """
    if isinstance(value, list) and not all(reader.is_validated_as_form_data for reader in readers):
        return False
    return not any(
        fields_of_readers[reader].takes_repeated_values(field)
        for reader in readers
        if reader.is_validated_as_form_data
        for field in fields_of_readers[reader].fields_of_keys.get(key, ())
    )


def _get_keys_to_switch(  # pragma: no cover # FastAPI >= 0.153
    wrong_keys: set[str],
    keys: list[str],
    readers_of_keys: dict[str, list[_FormModelReader]],
    fields_of_readers: "dict[_FormModelReader, _FormModelFields]",
) -> list[str]:
    """Return the wrong keys, and then the other aliases of the fields of the wrong keys, in the order of the body."""
    related_keys = {
        alias
        for key in wrong_keys
        for reader in readers_of_keys[key]
        for field in fields_of_readers[reader].fields_of_keys.get(key, ())
        for alias in fields_of_readers[reader].aliases_of_field(field)
    }
    return [key for key in keys if key in wrong_keys] + [
        key for key in keys if key in related_keys and key not in wrong_keys
    ]


def _as_form_value(value: Any, *, as_single_value: bool) -> Any:  # pragma: no cover # FastAPI >= 0.153
    if as_single_value:
        # FastAPI takes any value that isn't a list as a single form value
        return [value] if isinstance(value, list) else value
    return value if isinstance(value, list) else list(value)


def _describe_form_union_value_error(  # pragma: no cover # FastAPI >= 0.153
    key: str, value: Any, readers: list[_FormModelReader]
) -> str:
    model_names = ", ".join(
        f"`{reader.model.__name__}`" + ("" if reader.is_validated_as_form_data else " outside the union")
        for reader in readers
    )
    return (
        f"Cadwyn can't pass the {type(value).__name__} in {key!r} of the request body to every form model that reads "
        f"it ({model_names}). FastAPI validates the models of a union of form models from the body as if it were form "
        "data, so a field that takes a single form value would get only the last item of a list, a field that takes "
        "repeated form values would get a set or a tuple as one item, and a form model outside the union would get a "
        f"list in a list. Make {key!r} take repeated values (e.g. `list[str]`) in all of these models or in none of "
        "them, or don't put a list, set, or tuple into it in a validator or a migration."
    )


def _form_model_gets_value(  # pragma: no cover # FastAPI >= 0.153
    fields: "_FormModelFields", key: str, value: Any, model_body: dict[str, Any]
) -> bool:
    """Return whether the form model gets exactly the value from the dict that FastAPI validates it from.

    A list may be another list with the same items. A set, a frozenset, a tuple, a deque, or a subclass of a list may
    be a list of its items only if every field that Pydantic validates from the key takes repeated form values, so
    that Pydantic turns the list back into the field's type. Pydantic validates a field from the first of its aliases
    that the dict has.
    """
    result = model_body.get(key, PydanticUndefined)
    if result is value:
        return True
    items = value if type(value) is list else list(value)
    if type(result) is not list or len(result) != len(items):
        return False
    if any(result_item is not item for result_item, item in zip(result, items, strict=True)):
        return False
    if type(value) is list:
        return True
    fields_of_key = [
        field
        for field in fields.fields_of_keys.get(key, ())
        if not any(alias in model_body for alias in fields.aliases_before(field, key))
    ]
    return bool(fields_of_key) and all(fields.takes_repeated_values(field) for field in fields_of_key)


@dataclasses.dataclass(frozen=True)
class _FormModelFields:  # pragma: no cover # FastAPI >= 0.153
    """How FastAPI and Pydantic take the keys of form data for the fields of a form model. It depends only on the model.

    The fields are FastAPI's cached fields of the model, so we key them by identity.
    """

    allows_extra_fields: bool
    # The fields that FastAPI takes each key as an alias of
    fields_of_keys: dict[str, tuple[ModelField, ...]]
    # The aliases of each field, in the order in which Pydantic looks them up
    aliases_of_fields: dict[int, tuple[str, ...]]
    repeated_value_fields: frozenset[int]
    # The keys that Pydantic reads through an `AliasPath`, which FastAPI takes as extra fields
    alias_path_keys: frozenset[str]

    def reads(self, key: str) -> bool:
        return self.allows_extra_fields or key in self.fields_of_keys or key in self.alias_path_keys

    def aliases_of_field(self, field: ModelField) -> tuple[str, ...]:
        return self.aliases_of_fields[id(field)]

    def aliases_before(self, field: ModelField, key: str) -> tuple[str, ...]:
        aliases = self.aliases_of_field(field)
        return aliases[: aliases.index(key)]

    def takes_repeated_values(self, field: ModelField) -> bool:
        return id(field) in self.repeated_value_fields


_FORM_MODEL_FIELDS: "weakref.WeakKeyDictionary[type[BaseModel], _FormModelFields]" = weakref.WeakKeyDictionary()


def _get_form_model_fields(model: type[BaseModel]) -> _FormModelFields:  # pragma: no cover # FastAPI >= 0.153
    fields = _FORM_MODEL_FIELDS.get(model)
    if fields is None:
        fields = _FORM_MODEL_FIELDS[model] = _build_form_model_fields(model)
    return fields


def _build_form_model_fields(model: type[BaseModel]) -> _FormModelFields:  # pragma: no cover # FastAPI >= 0.153
    """Find the aliases of the fields by asking FastAPI about each name that Pydantic could validate a field from."""
    fields_of_keys: dict[str, list[ModelField]] = {}
    aliases_of_fields: dict[int, tuple[str, ...]] = {}
    repeated_value_fields: set[int] = set()
    alias_path_keys: set[str] = set()
    for field in _get_cached_model_fields(model):
        validation_alias = field.field_info.validation_alias
        choices = validation_alias.choices if isinstance(validation_alias, AliasChoices) else [validation_alias]
        alias_path_keys.update(
            choice.path[0] for choice in choices if isinstance(choice, AliasPath) and isinstance(choice.path[0], str)
        )
        possible_keys: list[Union[str, None]] = [choice for choice in choices if isinstance(choice, str)]
        possible_keys += [field.field_info.alias, field.alias, field.name]
        aliases = tuple(
            key
            for key in dict.fromkeys(possible_keys)
            if isinstance(key, str)
            and any(
                alias == key and item_field is field
                for alias, _, item_field in _extract_form_items([field], FormData([(key, "value")]))
            )
        )
        aliases_of_fields[id(field)] = aliases
        for alias in aliases:
            fields_of_keys.setdefault(alias, []).append(field)
        if aliases and isinstance(
            _get_multidict_value(field, FormData([(aliases[0], "a"), (aliases[0], "b")]), aliases[0]), list
        ):
            repeated_value_fields.add(id(field))
    return _FormModelFields(
        allows_extra_fields=model.model_config.get("extra") == "allow",
        fields_of_keys={key: tuple(fields) for key, fields in fields_of_keys.items()},
        aliases_of_fields=aliases_of_fields,
        repeated_value_fields=frozenset(repeated_value_fields),
        alias_path_keys=frozenset(alias_path_keys),
    )


def _unembed_form_model_errors(errors: list[Any], form_model_aliases: tuple[str, ...]) -> list[Any]:
    """Return the errors at the locations that FastAPI reports for form models that it validates from forms."""
    return [
        {**error, "loc": ("body", *error["loc"][2:])}
        if len(error["loc"]) > 1 and error["loc"][0] == "body" and error["loc"][1] in form_model_aliases
        else error
        for error in errors
    ]


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

    Before FastAPI 0.157.0, FastAPI reads these files to the end, so the head route would get them empty if we passed
    them on as is. Since then, FastAPI puts them back where they were, and rewinding them replays what the versioned
    route did with them.
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

    Before FastAPI 0.157.0, FastAPI reads these files to the end. A file that is elsewhere was moved by a
    dependency after that, e.g. to skip a header, so we leave it as it is. We check the end by seeking, which doesn't
    read the file.
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


def _get_strict_content_type(request: FastapiRequest) -> bool:
    """Return the `strict_content_type` with which FastAPI has read the body of the request.

    We resolve it like FastAPI does for the route that serves the request, which isn't the versioned route that
    Cadwyn has generated. A route in a tree of included routers gets its handler from its effective route context,
    which merges the values of the route and the routers that include it, and any other route has its own value.
    FastAPI's request handler takes the value of a default placeholder.
    """
    served_route = request.scope["route"]
    effective_route_context = _get_scope_effective_route_context(request.scope)
    if effective_route_context is not None and effective_route_context.original_route is served_route:
        strict_content_type = effective_route_context.strict_content_type
    else:
        strict_content_type = served_route.strict_content_type
    # E.g. a router that a mounted router includes keeps the placeholder, because no app gives it a bool
    if isinstance(strict_content_type, DefaultPlaceholder):
        strict_content_type = strict_content_type.value
    if not isinstance(strict_content_type, bool):  # pragma: no cover # FastAPI only has bools here
        raise CadwynError(
            f"Cadwyn can't tell how FastAPI has read the request body: the route that serves the request has "
            f"strict_content_type={strict_content_type!r} instead of True or False. Please, open an issue on Cadwyn's "
            "GitHub."
        )
    return strict_content_type


_UPLOADS_TO_CLOSE_SCOPE_KEY = "cadwyn.uploads_to_close"
# How long we wait for each upload to close, so that an upload that never closes can't keep the request running forever
_UPLOAD_CLOSE_TIMEOUT_SECONDS = 3
_UPLOAD_CLOSE_POLL_INTERVAL_SECONDS = 0.01


async def _close_file_in_thread(close: Callable[[], None]) -> None:
    finished = threading.Event()
    errors: list[BaseException] = []

    def run_close() -> None:
        try:
            close()
        except BaseException as exc:  # noqa: BLE001 # Keep abandoned worker failures out of threading.excepthook.
            errors.append(exc)
            _logger.debug("Cadwyn couldn't close an uploaded file: %s", exc)
        finally:
            finished.set()

    # A permanently blocked close must not keep Python alive after the event loop and ASGI shutdown finish.
    threading.Thread(target=copy_context().run, args=(run_close,), daemon=True).start()
    while not finished.is_set():  # noqa: ASYNC110 # Foreign threads cannot set an AnyIO event directly.
        # Poll a thread-safe signal without relying on a particular event loop or AnyIO version.
        await anyio.sleep(_UPLOAD_CLOSE_POLL_INTERVAL_SECONDS)
    if errors:
        raise errors[0]


async def _close_upload(upload: UploadFile) -> None:
    if (
        getattr(upload.close, "__func__", None) in (UploadFile.close, fastapi.UploadFile.close)
        and not upload._in_memory
    ):
        # Starlette's worker shields cancellation and can keep the process alive even if its wait is abandoned.
        await _close_file_in_thread(upload.file.close)
    else:
        # Keep in-memory closing and user-defined async close overrides in their original context.
        await upload.close()


class _UploadsToClose:
    """The uploads of a request that Cadwyn closes once the request is done, even if it gets cancelled.

    FastAPI closes the uploads of the form that it has parsed, but it doesn't shield closing them. So when the
    request gets cancelled, e.g. by a `BaseHTTPMiddleware` (like Cadwyn's) after the client has disconnected,
    the uploaded files stay open. FastAPI also doesn't know about the uploads that migrations put into the form,
    including the ones that later migrations replace or remove.
    """

    __slots__ = ("_deferred_files", "_uploads")

    def __init__(self) -> None:
        super().__init__()
        # Keyed by id, so that we close every upload once. The values keep the ids valid
        self._uploads: dict[int, UploadFile] = {}
        self._deferred_files: dict[int, BinaryIO] = {}

    def add(self, form: Iterable[tuple[str, Union[UploadFile, str]]]) -> None:
        for _, value in form:
            if isinstance(value, UploadFile):
                self._uploads.setdefault(id(value), value)

    def defer(self, form: Iterable[tuple[str, Union[UploadFile, str]]]) -> None:
        # Hold the streams, not just their ids: aliases must not retry a close whose worker may still be running.
        for _, value in form:
            if isinstance(value, UploadFile):
                self._deferred_files.setdefault(id(value.file), value.file)

    async def close(self) -> None:
        # Background tasks and streams have finished by now, so nothing reads the uploads anymore. If FastAPI closes
        # its uploads after us, closing them again does nothing
        for upload in self._uploads.values():
            file = upload.file
            if id(file) in self._deferred_files or getattr(file, "closed", False):
                continue
            with anyio.move_on_after(_UPLOAD_CLOSE_TIMEOUT_SECONDS, shield=True) as scope:
                try:
                    await _close_upload(upload)
                finally:
                    if scope.cancel_called:
                        self._deferred_files.setdefault(id(file), file)
                        _logger.warning(
                            "Cadwyn couldn't close the upload %r within %s seconds after the request, "
                            "so it may stay open",
                            upload.filename,
                            _UPLOAD_CLOSE_TIMEOUT_SECONDS,
                        )


def _get_uploads_to_close(request: FastapiRequest) -> _UploadsToClose:
    uploads: Union[_UploadsToClose, None] = request.scope.get(_UPLOADS_TO_CLOSE_SCOPE_KEY)
    if uploads is None:
        # Outside of a Cadwyn app (e.g. with the routers of `generate_versioned_routers()`), we close them with the
        # stack that FastAPI closes its forms with
        uploads = _UploadsToClose()
        request.scope[_UPLOADS_TO_CLOSE_SCOPE_KEY] = uploads
        request.scope["fastapi_middleware_astack"].push_async_callback(uploads.close)
    return uploads


def _run_request_migration(
    migration: Callable[[RequestInfo], None], request_info: RequestInfo, uploads: Union[_UploadsToClose, None]
) -> None:
    try:
        migration(request_info)
    finally:
        # A later migration can replace or remove the uploads that this one has put into the form
        if uploads is not None and request_info._form is not None:
            uploads.add(request_info._form)


# We use this instead of `.body()` to automatically guess body type and load the correct body, even if it's a form
async def _get_body(
    request: FastapiRequest, body_field: Union[ModelField, None], *, strict_content_type: bool
):  # pragma: no cover # This is from FastAPI
    is_body_form = body_field and isinstance(body_field.field_info, params.Form)
    try:
        body: Any = None
        if body_field:
            if is_body_form:
                # FastAPI has already parsed this form, so this returns its cached form. We close its files after the
                # response (`_UploadsToClose`), so background tasks can still read them
                body = await request.form()
            else:
                body_bytes = await request.body()
                if body_bytes:
                    json_body: Any = PydanticUndefined
                    content_type_value = request.headers.get("content-type")
                    if not content_type_value:
                        # Like FastAPI, we parse a body without a content type as JSON only if it's not strict
                        if not strict_content_type:
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


_SchemaInstruction = TypeVar("_SchemaInstruction")


class _SchemaMigrationTarget(NamedTuple):
    """What the schema migrations of a request body or a response model are looked up by."""

    # The annotations whose migrations the body or the response gets as a whole, e.g. the model `A` or `Union[A, B]`.
    # Their migrations replace the ones of the union members, so that none of them runs twice
    keys: tuple[object, ...] = ()
    # The latest versions of the models that are members of the annotation if it's a union, e.g. `Optional[A]`
    union_members: tuple[type[BaseModel], ...] = ()


def _get_schema_migration_target(annotation: object) -> _SchemaMigrationTarget:
    """Return what the schema migrations of a body or a response with this annotation are looked up by.

    Annotated and named type aliases (e.g. `TypeAliasType("Alias", Union[A, B])`) mean the same as their value.
    """
    resolved = _resolve_type_aliases(annotation)
    keys: dict[object, None] = {}
    for candidate in (annotation, resolved):
        if lenient_issubclass(candidate, BaseModel):
            candidate = getattr(candidate, "__cadwyn_original_model__", candidate)
        key = _get_schema_migration_key(candidate)
        if key is not None:
            keys[key] = None
    return _SchemaMigrationTarget(tuple(keys), _get_union_members(resolved))


def _resolve_type_aliases(annotation: object) -> object:
    """Return the annotation without the Annotated and the named type aliases around it."""
    seen_aliases: set[int] = set()
    while True:
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        elif is_typealiastype(annotation) and id(annotation) not in seen_aliases:
            seen_aliases.add(id(annotation))
            annotation = _get_type_alias_value(annotation)
        else:
            return annotation


def _get_union_members(annotation: object) -> tuple[type[BaseModel], ...]:
    """Return the latest versions of the models that a union annotation (e.g. `Optional[Union[A, B]]`) has as members.

    Other annotations, including models in lists, dicts, or generics, have none: migrations don't run for them.
    """
    annotation = _resolve_type_aliases(annotation)
    if not isinstance(annotation, UnionType):
        return ()
    members: dict[type[BaseModel], None] = {}
    for arg in get_args(annotation):
        arg = _resolve_type_aliases(arg)
        if isinstance(arg, UnionType):
            members.update(dict.fromkeys(_get_union_members(arg)))
        elif lenient_issubclass(arg, BaseModel):
            members[getattr(arg, "__cadwyn_original_model__", arg)] = None
    return tuple(members)


def _get_models_of_schema(schema: object) -> tuple[type[BaseModel], ...]:
    """Return the models that a schema of a migration is: the model itself, or the members of a union."""
    if lenient_issubclass(schema, BaseModel):
        return (schema,)
    return _get_union_members(schema)


def _has_union_member_migrations(target: _SchemaMigrationTarget, version_changes: "Iterable[dict[Any, Any]]") -> bool:
    """Whether any of these schema instructions of version changes is for a member of the target's union."""
    return any(member in instructions for instructions in version_changes for member in target.union_members)


def _get_schema_migrations(
    schema_instructions: "dict[Any, list[_SchemaInstruction]]",
    target: _SchemaMigrationTarget,
    get_union_member: "Callable[[], Union[type[BaseModel], None]]",
) -> "list[_SchemaInstruction]":
    """Return the schema migrations of a version change for a body or a response with this target.

    We only look up the member if one of the members has migrations, because that can fail.
    """
    for key in target.keys:
        if key in schema_instructions:
            return schema_instructions[key]
    if any(member in schema_instructions for member in target.union_members):
        member = get_union_member()
        if member is not None and member in schema_instructions:
            return schema_instructions[member]
    return []


def _no_union_member() -> None:
    """Find no union member: for the bodies and responses whose union members have no migrations."""


# Marks that the body param doesn't get the request body as an endpoint argument, so we don't have its validated value
_UNKNOWN_BODY = object()


def _get_closest_union_member(
    value: BaseModel, members: tuple[type[BaseModel], ...], annotation: object
) -> Union[type[BaseModel], None]:
    """Return the member of the union that the model object is, or None if it's no member.

    A subclass of a member, such as an internal representation, gets the migrations of that member. A model that is
    a subclass of several members that don't inherit from each other is ambiguous, so we fail instead of guessing.
    """
    value_type = type(value)
    model = getattr(value_type, "__cadwyn_original_model__", value_type)
    matches = [member for member in members if issubclass(model, member)]
    closest = [match for match in matches if all(issubclass(match, other) for other in matches)]
    if len(closest) == 1:
        return closest[0]
    if not matches:
        return None
    raise CadwynError(
        f"Cadwyn can't tell which member of {annotation!r} the {value_type.__name__} object is because it is "
        f"several of them: {[match.__name__ for match in matches]}. So it can't tell which of their schema "
        "migrations to run. Use an object of only one of them, or migrate this endpoint by its path instead."
    )


def _get_union_member_of_body(
    validated_body: object, members: tuple[type[BaseModel], ...], annotation: object
) -> Union[type[BaseModel], None]:
    """Return the member of the union that the body that the request's version has validated is, if any."""
    if not isinstance(validated_body, BaseModel):
        return None
    return _get_closest_union_member(validated_body, members, annotation)


def _raise_unknown_union_member_of_body(route: APIRoute, body_field_alias: Union[str, None]) -> NoReturn:
    body_annotation = route.body_field.field_info.annotation if route.body_field is not None else None
    raise CadwynError(
        f"Cadwyn can't tell which member of {body_annotation!r} the body of {sorted(route.methods or ())} "
        f'{route.path} is because the endpoint doesn\'t get the body as its "{body_field_alias}" argument, e.g. '
        "because the body param has an alias. So it can't tell which of their schema migrations to run. Give the "
        "body param of the endpoint no alias, or migrate this endpoint by its path instead."
    )


def _get_request_body_target(head_body_field: object, head_route: APIRoute) -> _SchemaMigrationTarget:
    """Return what the schema migrations of the body of a route with a single body param are looked up by.

    `head_body_field` is the model of the body of the route's version in the latest version. A union keeps the models
    of the route's version, so we look it up by the annotation of the latest version's route instead.
    """
    if head_body_field is None:
        return _SchemaMigrationTarget()
    annotation = head_route.body_field.field_info.annotation if head_route.body_field is not None else head_body_field
    target = _get_schema_migration_target(annotation)
    key = _get_schema_migration_key(head_body_field)
    if key is None or key in target.keys:
        return target
    return target._replace(keys=(key, *target.keys))


def _get_union_member_of_response(
    returned: object, members: tuple[type[BaseModel], ...], route: APIRoute, head_route: APIRoute
) -> Union[type[BaseModel], None]:
    """Return the member of the union that the endpoint has returned.

    FastAPI validates the response after the migrations, with the models of the client's version, so we can't reuse
    that validation. Validating the response before the migrations would run the validators of its models twice, so
    we only tell the member by the type of what the endpoint returns: a model object (or None, which is no member).
    """
    if returned is None:
        return None
    if isinstance(returned, BaseModel):
        member = _get_closest_union_member(returned, members, head_route.response_model)
        if member is not None:
            return member
    if isinstance(returned, FastapiResponse):
        reason = f"has returned a {type(returned).__name__}, which FastAPI doesn't validate"
    else:
        reason = (
            f"has returned a {type(returned).__name__} object instead of an object of a member, and Cadwyn would have "
            "to validate it to find out which member it becomes, which would run its validators twice"
        )
    raise CadwynError(
        f"Cadwyn can't tell which member of {head_route.response_model!r} the response of "
        f"{sorted(route.methods or ())} {route.path} is because the endpoint {reason}. So it can't tell which of "
        "their schema migrations to run. Return an object of the member's model instead, or migrate this endpoint "
        "by its path instead."
    )


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
    return _restore_surely_failing_data(spec, data)


def _rescue_model_data(model: type[BaseModel], data: Any) -> Any:
    """Restore the validation aliases of `model_dump(by_alias=True)` data only if `model.model_validate` surely fails.

    It's what `_rescue_body` does for a body field, for data that the model itself validates.
    """
    # Like `_rescue_body`, we go by the failing keys of `_get_failing_keys`, which come from the field infos even for
    # fields that the core schemas of their types give defaults to: such a field wouldn't fail without its value,
    # but it would silently get its default instead of the value in the body
    if not _annotation_reaches_models_with(model, _get_failing_renames, all_fields=False):
        return data
    return _restore_surely_failing_data(("model", model), data)


def _restore_surely_failing_data(spec: "_RestorerSpec", data: Any) -> Any:
    """Restore the data only if it surely fails validation as is, so that we decide what to validate before validating.

    Validators can have side effects, so the data is validated only once, whichever data it is.
    """
    _RESTORATION.has_failing_renames = False
    restored = _restore_data(spec, data)
    return restored if _RESTORATION.has_failing_renames else data


def _get_route_json_values_plan(
    head_dependant: Dependant, route: APIRoute, *, embed_body_fields: bool
) -> "Union[_JsonValuesPlan, None]":
    if embed_body_fields or len(head_dependant.body_params) != 1 or len(route.dependant.body_params) != 1:
        return None
    return _get_json_values_plan(head_dependant.body_params[0], route.dependant.body_params[0])


@dataclasses.dataclass(slots=True)
class _ClientJsonStrings:
    """The strings that the client has sent in `Json[...]` fields, by their paths in the body.

    We put new copies of them into the body, which nothing else can have. Python shares some strings, though, e.g.
    the empty string, and some versions of pydantic decode strings of one character into the ones that Python
    shares, so we can't copy them. A migration might set the same string objects, so we only treat them as the
    client's if no migration has run, and then nothing can have changed them.
    """

    by_path: "dict[_JsonPath, str]" = dataclasses.field(default_factory=dict)
    shared_paths: "set[_JsonPath]" = dataclasses.field(default_factory=set)

    def forget_shared_strings(self) -> None:
        for path in self.shared_paths:
            del self.by_path[path]


def _copy_decoded_json_strings(plan: "Union[_JsonValuesPlan, None]", body: Any, strings: _ClientJsonStrings) -> Any:
    """Return the body of the request's version with new copies of the strings that it has decoded from JSON.

    The head would decode these strings once more, so we encode them, e.g. `"123"` as `'"123"'`. But a string that
    a migration sets might be JSON already (e.g. one that it has encoded to work around the 500s of `Json[...]`
    fields before 8.2.0), so we remember the copies that we put into the body by their paths in `strings`, and only
    encode a string if it is the very copy that we have put at its path. Python and pydantic share other strings
    (e.g. `"5"`, or the strings that pydantic caches as it decodes JSON) with whatever code makes the same ones,
    and a migration can still get a copy, e.g. with `sys.intern()`, but not put it at its path by accident.
    It skips the containers that it doesn't expect, e.g. in a default that FastAPI gives without validating it.
    """
    if plan is None:
        return body
    return _replace_planned_json_values(
        plan, body, functools.partial(_copy_decoded_json_string, strings=strings), (), is_strict=False
    )


def _copy_decoded_json_string(_check: Any, value: Any, path: "_JsonPath", *, strings: _ClientJsonStrings) -> Any:
    if type(value) is not str:
        return value
    if len(value) > 1:
        copy = value[:1] + value[1:]
    elif value and ord(value) > 0xFF:
        copy = chr(ord(value))
    else:
        # Python shares the empty string and the strings of one character up to "\xff", but some versions of
        # pydantic decode them anew (e.g. "5"). We can't copy the shared ones.
        copy = from_json(to_json(value), cache_strings=False)
        if copy is value or (value and copy is chr(ord(value))):
            strings.by_path[path] = value
            strings.shared_paths.add(path)
            return value
    strings.by_path[path] = copy
    return copy


# How the head validates the data of a body schema, as far as the models of the request's version in it go:
# `("model", cls, head_cls)` for a model of the request's version and the head's model at the same place,
# `("items", plan)` for the items of a list, `("values", plan)` for the values of a dict, and `("union", plans)` for
# the members of a union. None means that the data stays as it is.
_InputPlan: TypeAlias = Union[tuple[Any, ...], None]
# The plans of the pairs of a body field of a version and the head's body field, by the ids of the fields
_BODY_INPUT_PLANS: dict[tuple[int, int], tuple[ModelField, ModelField, _InputPlan]] = {}


@dataclasses.dataclass(frozen=True)
class _ModelInputPlan:
    # (name, key, plan) of the fields: the key that the head validates the field from, and the plan of its value
    fields: tuple[tuple[str, str, _InputPlan], ...] = ()
    # The keys of the data that any field validates, which the extra keys must not have
    validated_keys: frozenset[Union[str, int]] = frozenset()


def _to_head_input(
    route: APIRoute, head_dependant: Dependant, head_route: APIRoute, body: Any, *, embed_body_fields: bool
) -> Any:
    """Return the migrated body with the models of the request's version in it replaced by the data of their fields.

    Bodies that aren't models themselves, such as `list[Model]`, reach migrations as FastAPI validates them, with
    the models of the request's version in them. FastAPI validates such models for the head with
    `from_attributes=True`, which only reads the attributes of their fields by their validation aliases. So the head
    would lose their extra keys, take all of their fields as set, and miss the fields with aliases.

    So we give the head the data that the client has sent instead: the values of the fields that are set, under the
    keys that the head validates them from, and the extra keys. We take the values as they are, and only walk into
    the places where both the body's schema and the head's have models, through lists, dicts, and unions without
    validators or serializers in either of them. Everything else, e.g. the values of `Any` fields, the models that
    a validator returns, the models of the head that a migration adds, tuples, sets, and root models, stays as it is.
    We only copy the lists and dicts in which something changes.

    Every param of the head's dependencies that reads the body gets the same data, so we only change it if all of them
    validate it by the same plan as the endpoint's body param.
    """
    if embed_body_fields or len(route.dependant.body_params) != 1 or len(head_dependant.body_params) != 1:
        return body
    body_field = route.dependant.body_params[0]
    plan = _get_body_input_plan(body_field, head_dependant.body_params[0])
    if plan is None or not _do_body_readers_share_plan(body_field, head_dependant, head_route, plan):
        return body
    # Recursive schemas let the data be as deep as pydantic accepts. Nothing here has side effects, so we can just
    # leave such a body as it is.
    try:
        return _to_input_by_plan(body, plan)
    except RecursionError:  # pragma: no cover # Python 3.12+ raises it only for bodies that pydantic rejects
        return body


def _do_body_readers_share_plan(
    body_field: ModelField, head_dependant: Dependant, head_route: APIRoute, plan: _InputPlan
) -> bool:
    """Whether every param in the head's dependency tree that reads the body validates it by the plan.

    If the app has dependency overrides, FastAPI builds the dependants of all dependencies again on each request, from
    what they are then (e.g. their signatures), which runs code of users (e.g. the schema hooks of their types). We
    would have to build them once more to check them, so we leave the body as it is in such apps. Without overrides,
    FastAPI solves the dependants that it has built for the route, which are the ones that we check.
    """
    if _get_dependency_overrides(head_route):
        return False
    return all(
        _get_body_input_plan(body_field, reader_field) == plan
        for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
        for reader_field in dependant.body_params
    )


def _get_body_input_plan(body_field: ModelField, head_body_field: ModelField) -> _InputPlan:
    cached = _BODY_INPUT_PLANS.get((id(body_field), id(head_body_field)))
    if cached is not None and cached[0] is body_field and cached[1] is head_body_field:
        return cached[2]
    try:
        plan = _get_input_plan(
            _register_core_schema_definitions(cast("dict[str, Any]", body_field._type_adapter.core_schema)),
            _register_core_schema_definitions(cast("dict[str, Any]", head_body_field._type_adapter.core_schema)),
        )
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas
        plan = None
    _BODY_INPUT_PLANS[id(body_field), id(head_body_field)] = (body_field, head_body_field, plan)
    return plan


def _get_input_plan(
    schema: dict[str, Any],
    head_schema: dict[str, Any],
    visiting: frozenset[str] = frozenset(),
    head_visiting: frozenset[str] = frozenset(),
) -> _InputPlan:
    """Return where the data of the request's version's core schema has models that the head's has at the same place.

    The way to them must have no validators or serializers in either of the schemas.
    """
    unwrapped, head_unwrapped = _unwrap_input_schema(schema, visiting), _unwrap_input_schema(head_schema, head_visiting)
    if unwrapped is None or head_unwrapped is None:
        return None
    (schema, visiting), (head_schema, head_visiting) = unwrapped, head_unwrapped
    if _UNION_SCHEMA_TYPES.intersection((schema["type"], head_schema["type"])):
        return _get_union_input_plan(schema, head_schema, visiting, head_visiting)
    if _get_schema_shape(schema) is None or _get_schema_shape(schema) != _get_schema_shape(head_schema):
        return None
    schema_type = schema["type"]
    if schema_type == "model":
        return "model", schema["cls"], head_schema["cls"]
    child_key = "values_schema" if schema_type == "dict" else "items_schema"
    plan = _get_input_plan(schema[child_key], head_schema[child_key], visiting, head_visiting)
    if plan is None:
        return None
    return ("values", plan) if schema_type == "dict" else ("items", plan)


_UNION_SCHEMA_TYPES = frozenset(("union", "tagged-union"))


def _get_schema_shape(schema: dict[str, Any]) -> object:
    """Return what kind of data an unwrapped core schema validates into, or None if we don't walk into such data.

    Models have the shape of the head's model that they are copies of. We don't walk into tuples, sets, and other
    containers: pydantic validates their data into them, and strict schemas reject lists, which our copies would be.
    """
    schema_type = schema["type"]
    if schema_type == "model":
        return "model", getattr(schema["cls"], "__cadwyn_original_model__", schema["cls"])
    if schema_type in ("list", "dict"):
        return schema_type
    return None


def _get_union_input_plan(
    schema: dict[str, Any], head_schema: dict[str, Any], visiting: frozenset[str], head_visiting: frozenset[str]
) -> _InputPlan:
    """Return the plans of the members of the request's version's union that have one member of the same shape.

    The members are looked up in the head's union, or are the head's schema itself if it isn't a union.
    """
    head_members = _get_input_union_members(head_schema, head_visiting)
    plans: dict[_InputPlan, None] = {}
    for member, member_visiting in _get_input_union_members(schema, visiting):
        shape = _get_schema_shape(member)
        matching_head_members = [
            head_member
            for head_member in head_members
            if shape is not None and _get_schema_shape(head_member[0]) == shape
        ]
        if len(matching_head_members) != 1:
            continue
        head_member, head_member_visiting = matching_head_members[0]
        plan = _get_input_plan(member, head_member, member_visiting, head_member_visiting)
        for member_plan in plan[1] if plan is not None and plan[0] == "union" else (plan,):
            plans[member_plan] = None
    plans.pop(None, None)
    return ("union", tuple(plans)) if plans else None


def _get_input_union_members(
    schema: dict[str, Any], visiting: frozenset[str]
) -> list[tuple[dict[str, Any], frozenset[str]]]:
    """Return the unwrapped members of a union, including the ones of the unions in it, or the schema itself"""
    if schema["type"] not in _UNION_SCHEMA_TYPES:
        return [(schema, visiting)]
    choices = schema["choices"].values() if schema["type"] == "tagged-union" else schema["choices"]
    members: list[tuple[dict[str, Any], frozenset[str]]] = []
    for choice in choices:
        # Union choices can be `(schema, label)` tuples
        unwrapped = _unwrap_input_schema(choice[0] if isinstance(choice, tuple) else choice, visiting)
        if unwrapped is not None:
            members.extend(_get_input_union_members(*unwrapped))
    return members


def _unwrap_input_schema(
    schema: dict[str, Any], visiting: frozenset[str]
) -> Union[tuple[dict[str, Any], frozenset[str]], None]:
    """Return the schema without the references, optional values, and defaults around it, or None if it's unsafe.

    The own core schema of a model becomes the model schema itself.
    """
    while True:
        # References carry the serializers of `Annotated[Model, PlainSerializer(...)]`
        if _is_custom_serialization(schema.get("serialization")):
            return None
        if schema["type"] == "definition-ref":
            ref = schema["schema_ref"]
            # A recursive type alias of containers, e.g. `type Items = list[Union[Item, Items]]`
            if ref in visiting:
                return None
            visiting |= {ref}
            schema = _CORE_SCHEMA_DEFINITIONS[ref]
            continue
        # The own core schema of a model has its ref, and wraps it in the "after" validators of the model
        if schema.get("ref") is not None:
            model_schema = schema
            while model_schema["type"] == "function-after":
                model_schema = model_schema["schema"]
            if model_schema["type"] == "model":
                return model_schema, visiting
        if schema["type"] == "nullable" or (schema["type"] == "default" and schema.get("on_error", "raise") == "raise"):
            schema = schema["schema"]
            continue
        return schema, visiting


def _to_input_by_plan(data: Any, plan: _InputPlan) -> Any:
    if plan is None:
        return data
    kind = plan[0]
    if kind == "union":
        # We only walk into the member that the data surely belongs to
        matching_plans = [member for member in plan[1] if _plan_matches(member, data)]
        return _to_input_by_plan(data, matching_plans[0]) if len(matching_plans) == 1 else data
    if not _plan_matches(plan, data):
        return data
    if kind == "model":
        model_plan = _get_model_input_plan(plan[1], plan[2])
        return data if model_plan is None else _model_to_input(data, model_plan)
    if kind == "values":
        values = {key: _to_input_by_plan(value, plan[1]) for key, value in data.items()}
        has_changed = any(values[key] is not value for key, value in data.items())
        return values if has_changed else data
    items = [_to_input_by_plan(item, plan[1]) for item in data]
    return items if any(item is not original for item, original in zip(items, data, strict=True)) else data


def _plan_matches(plan: tuple[Any, ...], data: Any) -> bool:
    """Whether the data has the exact type that the plan's schema validates data into"""
    kind = plan[0]
    if kind == "model":
        return type(data) is plan[1]
    return type(data) is (list if kind == "items" else dict)


def _model_to_input(model: BaseModel, plan: _ModelInputPlan) -> Any:
    extra = model.__pydantic_extra__
    # The head would take an extra key that a field validates as the field's value
    if extra and not plan.validated_keys.isdisjoint(extra):
        return model
    fields_set = model.model_fields_set
    data = {
        key: _to_input_by_plan(getattr(model, name), field_plan)
        for name, key, field_plan in plan.fields
        if name in fields_set
    }
    if extra:
        data.update(extra)
    return data


@functools.cache
def _get_model_input_plan(model: type[BaseModel], head_model: type[BaseModel]) -> Union[_ModelInputPlan, None]:
    """Return how the head validates the data of the fields of a model of the request's version, if it surely does.

    Only copies of the head's models need it, because FastAPI takes the head's own models as they are. A copy must
    have the fields of the head's model at the same place with their keys and defaults: the head reads the attributes
    of the fields that it has, which keeps the defaults of the request's version and ignores the fields that only the
    request's version has, while it would get such fields from the data as extra keys. Both models must validate the
    data of their fields as a whole, without validators that get the raw input (e.g. `model_validator(mode="before")`)
    or a custom `__init__`, and we only walk into the values of fields whose schemas have models in both of them.
    Root models aren't converted at all: their validators get their data as a whole (e.g. a `mode="before"` validator
    of `RootModel[int]` gets the int), which they might transform once more.

    Every field gets the key that it's dumped to if the model validates it. A required field that the model validates
    from another key (e.g. `Field(alias="x", validation_alias="v")`) gets its first plain validation key, because the
    head surely rejects the data without it. The head might silently lose the values of other fields, so we leave
    their models as they are. Each key must belong to exactly one field, so that no field takes the value of another.
    """
    if (
        not hasattr(model, "__cadwyn_original_model__")
        # The head's model is a root model too, because both are versions of the same model
        or issubclass(model, RootModel)
        or not _has_fields_of_head_model(model, head_model)
    ):
        return None
    try:
        inner_schema, head_inner_schema = _get_model_inner_core_schema(model), _get_model_inner_core_schema(head_model)
        if inner_schema is None or head_inner_schema is None:
            return None
        field_schemas, head_field_schemas = inner_schema["fields"], head_inner_schema["fields"]
        # A custom `__get_pydantic_core_schema__` can validate other fields or keys than the field infos say
        if not _do_compiled_fields_match(model, field_schemas) or not _do_compiled_fields_match(  # pragma: no cover
            head_model, head_field_schemas
        ):
            return None
        fields: list[tuple[str, str, _InputPlan]] = []
        keys_of_fields: dict[str, frozenset[Union[str, int]]] = {}
        for name, field in model.model_fields.items():
            paths = _get_validation_paths(model, name, field)
            dumped_key = field.serialization_alias or name
            plain_keys = [path[0] for path in paths if len(path) == 1 and isinstance(path[0], str)]
            if [dumped_key] in paths:
                key = dumped_key
            elif field.is_required() and plain_keys:
                key = plain_keys[0]
            else:
                return None
            keys_of_fields[name] = frozenset(path[0] for path in paths)
            plan = _get_input_plan(field_schemas[name]["schema"], head_field_schemas[name]["schema"])
            fields.append((name, key, plan))
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas
        return None
    if any(
        key in other_keys
        for name, key, _ in fields
        for other_name, other_keys in keys_of_fields.items()
        if other_name != name
    ):
        return None
    return _ModelInputPlan(fields=tuple(fields), validated_keys=frozenset().union(*keys_of_fields.values()))


def _do_compiled_fields_match(model: type[BaseModel], field_schemas: dict[str, Any]) -> bool:
    return set(field_schemas) == set(model.model_fields) and all(
        _do_compiled_aliases_match(field, field_schemas[name]) for name, field in model.model_fields.items()
    )


def _has_fields_of_head_model(model: type[BaseModel], head_model: type[BaseModel]) -> bool:
    """Whether a copy of a model has the fields of the head's model, with the same keys and defaults"""
    if model.model_fields.keys() != head_model.model_fields.keys():
        return False
    for name, field in model.model_fields.items():
        head_field = head_model.model_fields[name]
        if (
            (field.serialization_alias or name) != (head_field.serialization_alias or name)
            or _get_validation_paths(model, name, field) != _get_validation_paths(head_model, name, head_field)
            or _unwrap_default_factory(field.default_factory) is not _unwrap_default_factory(head_field.default_factory)
            # Defaults can be equal but different, e.g. `Decimal("1.00")` and `Decimal("1.0")`
            or not is_same_value(field.default, head_field.default)
        ):
            return False
    return True


def _unwrap_default_factory(factory: object) -> object:
    # Copies of models wrap the default factories of the head's models. `schema_generation` imports this module.
    from cadwyn.schema_generation import _CallableWrapper

    return factory._original_callable if isinstance(factory, _CallableWrapper) else factory


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
    client_strings = client_json_strings.by_path
    # Whatever goes wrong, we leave the body as it is, because encoding must never break a request
    try:
        # Encoding copies containers and hashes their keys, and checking the dependencies builds the dependants of
        # the overrides, both of which run code of users (e.g. `__hash__` or schema hooks). So we only do them if
        # a read-only pass finds a value to encode, which makes the request fail or get a wrong value as it is.
        try:
            client_strings_check = None
            has_values_to_encode = _has_json_values_to_encode(
                plan, body, functools.partial(_is_json_value_to_encode, strings=client_strings), ()
            )
        # If we can't encode one of the values, e.g. a string that a migration has encoded already, a migration might
        # have encoded the others too, so we leave them as they are and only encode the strings that the client has
        # sent. Values that aren't strings fail in the head as they are, as before. But if a string that the client
        # has sent isn't at its place anymore (e.g. a migration has moved or removed it), or if we can't walk into
        # a part of the body (e.g. a tuple that a migration has put there), the head might decode a string that the
        # client has sent once more, so the whole body stays as it is. If we couldn't copy one of the strings that the
        # client has sent, we can't tell whether it is still at its place, so the whole body stays as it is too.
        except _UnsafeJsonValueError:
            if client_json_strings.shared_paths:
                raise
            client_strings_check = _ClientJsonStringsCheck(client_strings)
            has_values_to_encode = _has_json_values_to_encode(plan, body, client_strings_check, ())
            client_strings_check.check_that_all_are_in_place()
        if not has_values_to_encode or _has_other_body_readers(head_dependant, head_route):
            return body
        if client_strings_check is not None:
            # Checking the dependencies might have changed the body, so we check it again as we encode it
            client_strings_check = _ClientJsonStringsCheck(client_strings)
            encoded = _replace_planned_json_values(
                plan,
                body,
                lambda check, value, path: (
                    _encode_json_value(value) if client_strings_check(check, value, path) else value
                ),
                (),
                is_strict=True,
            )
            client_strings_check.check_that_all_are_in_place()
            return encoded
        return _replace_planned_json_values(
            plan,
            body,
            lambda check, value, path: (
                _encode_json_value(value)
                if _is_json_value_to_encode(check, value, path, strings=client_strings)
                else value
            ),
            (),
            is_strict=True,
        )
    except Exception:  # noqa: BLE001
        return body


def _has_other_body_readers(head_dependant: Dependant, head_route: APIRoute) -> bool:
    """Whether a dependency of the head reads the body with another type than the body param (e.g. a dict).

    It would read the JSON that we encode instead of what the client has sent. A dependency that reads the body into
    the same type validates it as the body param does. FastAPI builds the dependants of the overrides from what they
    are on each request (e.g. their signatures), so we check them on each request that we encode as well.
    """
    field_info = head_dependant.body_params[0].field_info
    return any(
        field.field_info.annotation is not field_info.annotation
        or len(field.field_info.metadata) != len(field_info.metadata)
        or any(
            item is not body_item
            for item, body_item in zip(field.field_info.metadata, field_info.metadata, strict=True)
        )
        for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
        if dependant is not head_dependant
        for field in dependant.body_params
    )


class _UnsafeJsonValueError(Exception):
    """The head might not get what the client has sent if we encode the `Json[...]` values of the body"""


# How the head reads the dumped data of a core schema, or None if there's nothing to describe (e.g. a plain value):
# `("json", check)` for a `Json[...]` value, `("nullable", description)`, `("items", description)`, and
# `("values", description)` for None, list or set items, and dict values, `("model", keys, ((key, description), ...))`
# for the data of a model with its dumped keys.
# In `Json[...]` values, it says what the parsed value must look like for us to encode it. The plan of what to encode
# is the description without the parts that have no `Json[...]` values.
_JsonValuesPlan: TypeAlias = tuple[Any, ...]
# Where a value is in the body: the keys of models, the types and keys of other dicts, and the indexes of lists
_JsonPath: TypeAlias = tuple[Any, ...]
# The keys of the child schemas of the core schema types that hold other values, and the kinds of their descriptions
_JSON_CHILD_SCHEMA_KEYS = {
    "nullable": ("schema", "nullable"),
    "list": ("items_schema", "items"),
    "dict": ("values_schema", "values"),
}
# The descriptions of the body fields, and the plans for the pairs of a head body field and a body field of a version
_BODY_FIELD_JSON_PLANS: dict[int, tuple[ModelField, Union[_JsonValuesPlan, None]]] = {}
_NORMALIZED_CORE_SCHEMAS: dict[int, tuple[ModelField, Any]] = {}
_JSON_VALUES_PLANS: dict[tuple[int, int], tuple[ModelField, ModelField, Union[_JsonValuesPlan, None]]] = {}
# The descriptions of the models of one body field by `(is_in_json, model, depth)`, so that describing stays linear
_JsonPlanMemo: TypeAlias = dict[tuple[bool, type[BaseModel], int], Any]
_UNSAFE_JSON_VALUE = object()
# The greatest number of nested models that we walk into, so that the first request of a deep body doesn't need
# a much deeper stack than the next ones
_MAX_JSON_PLAN_DEPTH = 8
# The core schema types without validators whose values the head reads as the client has sent them, both as they
# are and encoded as JSON, by the exact types of their values. Any other type that the walk doesn't know stops it.
_JSON_LEAF_VALUE_TYPES: dict[str, tuple[type, ...]] = {
    "none": (type(None),),
    "bool": (bool,),
    "int": (int,),
    "float": (float,),
    "decimal": (Decimal,),
    "str": (str,),
    "date": (date,),
    "time": (time,),
    "datetime": (datetime,),
    "timedelta": (timedelta,),
    "uuid": (uuid.UUID,),
}
_JSON_LEAF_SCHEMA_TYPES = frozenset((*_JSON_LEAF_VALUE_TYPES, "any", "literal", "enum"))
# The types of the values that JSON has
_JSON_NATIVE_TYPES = (str, int, float, bool, type(None))
# The core schema types whose values keep their JSON type when we encode them, so a union of them picks the same member
_PLAIN_JSON_SCHEMA_TYPES = frozenset(
    ("any", "none", "bool", "int", "float", "str", "literal", *_JSON_CHILD_SCHEMA_KEYS)
)
_PLAIN_LITERAL_TYPES = (str, int, bool, type(None))
# The keys of these core schemas that hold no constraints
_PLAIN_JSON_SCHEMA_KEYS = frozenset(
    ("type", "metadata", "expected", "schema", "items_schema", "keys_schema", "values_schema")
)
# The settings of models that change or constrain their strings, as the constraints of string schemas do
_STRING_CONFIG_KEYS = (
    *("str_to_lower", "str_to_upper", "str_strip_whitespace", "str_min_length", "str_max_length"),
    "coerce_numbers_to_str",
)
# Default factories that always give the same value
_DETERMINISTIC_DEFAULT_FACTORIES = (list, dict, set, frozenset, tuple)


def _get_json_values_plan(head_body_field: ModelField, body_field: ModelField) -> Union[_JsonValuesPlan, None]:
    """Return what to encode in the dump of the body field of the request's version, which the head validates.

    The request's version dumps the body, so its models must dump the values to the same keys that the head reads
    them from. Otherwise, e.g. if an older version excludes a field from the dump or dumps another field to its key,
    the head wouldn't get what the client has sent, so we leave the body as it is.
    """
    cached = _JSON_VALUES_PLANS.get((id(head_body_field), id(body_field)))
    if cached is not None and cached[0] is head_body_field and cached[1] is body_field:
        return cached[2]
    try:
        plan = _get_body_field_json_plan(head_body_field)
        # The models of the request's version are copies of the head's, so the core schemas are the same if the
        # version doesn't change the body, apart from the classes and the names of the definitions. Any other
        # difference, even a constraint of a string, could make the head read another value than the request's
        # version has.
        if (
            plan is not None
            and body_field is not head_body_field
            and _get_normalized_core_schema(body_field) != _get_normalized_core_schema(head_body_field)
        ):
            plan = None
    # Planning must never break a request that works without it, so whatever goes wrong (e.g. a default whose
    # `__eq__` raises), we leave the body as it is
    except Exception:  # noqa: BLE001
        plan = None
    _JSON_VALUES_PLANS[id(head_body_field), id(body_field)] = (head_body_field, body_field, plan)
    return plan


def _get_body_field_json_plan(body_field: ModelField) -> Union[_JsonValuesPlan, None]:
    cached = _BODY_FIELD_JSON_PLANS.get(id(body_field))
    if cached is not None and cached[0] is body_field:
        return cached[1]
    try:
        schema = cast("dict[str, Any]", body_field._type_adapter.core_schema)
        # Most bodies have no `Json[...]` fields, so they aren't walked at all
        description = (
            _walk_json_values(_register_core_schema_definitions(schema), frozenset(), {}, is_in_json=False)
            if _has_core_schema_of_types(schema, frozenset(("json",)))
            else None
        )
    except (_UnsafeJsonValueError, *_CORE_SCHEMA_ERRORS):
        description = None
    plan = _prune_json_values_description(description, {})
    _BODY_FIELD_JSON_PLANS[id(body_field)] = (body_field, plan)
    return plan


def _get_normalized_core_schema(body_field: ModelField) -> Any:
    cached = _NORMALIZED_CORE_SCHEMAS.get(id(body_field))
    if cached is None or cached[0] is not body_field:
        cached = (body_field, _normalize_core_schema(body_field._type_adapter.core_schema, {}))
        _NORMALIZED_CORE_SCHEMAS[id(body_field)] = cached
    return cached[1]


def _normalize_core_schema(value: Any, refs: dict[str, int]) -> Any:
    """Return the core schema with its classes by their names and its definitions by the order of their references.

    Every other value is tagged with its type, because values of different types can be equal in Python (e.g. `1`,
    `1.0`, and `True`) while pydantic tells them apart, e.g. in literals and defaults. Metadata (e.g. the functions
    that make JSON schemas) doesn't change how the schema validates.
    """
    if isinstance(value, dict):
        return {
            _normalize_core_schema(key, refs): refs.setdefault(item, len(refs))
            if key in ("ref", "schema_ref") and isinstance(item, str)
            else _normalize_core_schema(item, refs)
            for key, item in value.items()
            if key != "metadata"
        }
    # Containers stay hashable, so that they can be the keys of dicts (e.g. of defaults), and keep their types
    if isinstance(value, (list, tuple)):
        return type(value).__name__, tuple(_normalize_core_schema(item, refs) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value).__name__, frozenset(_normalize_core_schema(item, refs) for item in value)
    if isinstance(value, type):
        return "class", value.__module__, value.__qualname__
    return "value", type(value).__module__, type(value).__qualname__, value


def _prune_json_values_description(
    description: Union[_JsonValuesPlan, None], memo: dict[int, Union[_JsonValuesPlan, None]]
) -> Union[_JsonValuesPlan, None]:
    """Return the plan of what to encode: the parts of the description that lead to `Json[...]` values.

    The descriptions of models are shared by the places that have them, so we prune each of them once.
    """
    if description is None:
        return None
    if id(description) in memo:
        return memo[id(description)]
    if description[0] == "json":
        plan = description
    elif description[0] == "model":
        fields = tuple(
            (key, child_plan)
            for key, child in description[2]
            if (child_plan := _prune_json_values_description(child, memo)) is not None
        )
        plan = ("model", description[1], fields) if fields else None
    else:
        child = _prune_json_values_description(description[1], memo)
        plan = None if child is None else (description[0], child)
    memo[id(description)] = plan
    return plan


def _has_core_schema_of_types(schema: dict[str, Any], types: frozenset[str]) -> bool:
    """Whether the core schema, its definitions, or the ones it refers to have a schema of one of the types.

    It's found without recursion.
    """
    stack: list[Any] = [schema]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if isinstance(node, (dict, list, tuple)) and id(node) not in seen:
            seen.add(id(node))
            if not isinstance(node, dict):
                stack.extend(node)
            elif node.get("type") in types:
                return True
            elif node.get("type") == "definition-ref" and node.get("schema_ref") in _CORE_SCHEMA_DEFINITIONS:
                stack.append(_CORE_SCHEMA_DEFINITIONS[node["schema_ref"]])
            else:
                # Defaults, literal values, and metadata are data, not schemas
                stack.extend(value for key, value in node.items() if key not in ("default", "expected", "metadata"))
    return False


def _walk_json_values(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    """Describe how the head reads the dumped data of the core schema, or raise `_UnsafeJsonValueError`.

    We only encode the `Json[...]` values of a body if the head gets what the client has sent everywhere in it,
    so we raise for anything that might change or drop a value: validators (the dump has already been validated,
    so the head would change it once more), serializers, defaults that swallow errors or that the head computes once
    more, and the data of models that we can't check. In `Json[...]` values, the head reads JSON that we encode,
    so we also raise for anything whose JSON might differ from what the client has sent, e.g. some unions.
    """
    if _is_custom_serialization(schema.get("serialization")) or schema.get("on_error", "raise") != "raise":
        raise _UnsafeJsonValueError
    schema_type = schema["type"]
    if schema_type in _JSON_LEAF_SCHEMA_TYPES:
        return _walk_json_leaf(schema, is_in_json=is_in_json)
    if schema_type == "default":
        # The head calls the default factories of the fields that the dump leaves out once more
        if "default_factory" in schema and schema["default_factory"] not in _DETERMINISTIC_DEFAULT_FACTORIES:
            raise _UnsafeJsonValueError
        return _walk_json_values(schema["schema"], visiting, memo, is_in_json=is_in_json)
    walk = _JSON_SCHEMA_WALKERS.get(schema_type)
    if walk is not None:
        return walk(schema, visiting, memo, is_in_json=is_in_json)
    # We only walk into the schema types that we know, e.g. not into generators, whose unions we wouldn't check
    if schema_type not in _JSON_CHILD_SCHEMA_KEYS:
        raise _UnsafeJsonValueError
    child_key, kind = _JSON_CHILD_SCHEMA_KEYS[schema_type]
    if not is_in_json:
        # We don't encode the keys of dicts, so they mustn't have `Json[...]` values
        if "keys_schema" in schema and _walk_json_values(schema["keys_schema"], visiting, memo, is_in_json=False):
            raise _UnsafeJsonValueError
        child = _walk_json_values(schema[child_key], visiting, memo, is_in_json=False) if child_key in schema else None
        return None if child is None else (kind, child)
    # In `Json[...]` values, we check that the values that the schema doesn't type are JSON
    child = _walk_json_values(schema[child_key], visiting, memo, is_in_json=True) if child_key in schema else ("any",)
    if schema_type != "dict":
        return kind, child
    keys_check = (
        _walk_json_values(schema["keys_schema"], visiting, memo, is_in_json=True)
        if "keys_schema" in schema
        else ("any",)
    )
    # The keys of JSON objects are strings, so keys of several types (e.g. `1` and `"1"`) could become the same key.
    # We only allow keys of a single type, and check at runtime that their JSON differs (e.g. for NaN).
    if keys_check[0] not in ("leaf", "enum", "any") or (keys_check[0] != "any" and len(keys_check[-1]) != 1):
        raise _UnsafeJsonValueError
    return kind, child, keys_check


def _walk_json_leaf(schema: dict[str, Any], *, is_in_json: bool) -> Union[_JsonValuesPlan, None]:
    """Describe the exact types of the values of a leaf in a `Json[...]` value, so that we can check them.

    A migration can set values of other types, which encoding might change, e.g. a decimal that becomes a string.
    Outside of `Json[...]` values, the head validates the dumped values themselves, as it does without `Json[...]`.
    """
    schema_type = schema["type"]
    if schema_type == "enum":
        values = [member.value for member in schema["members"]]
        # The head looks the members up by their values, which must stay what they are when we encode them (e.g.
        # a decimal becomes a string, which might be the value of another member), and a custom lookup could pick
        # another member. As dict keys, values of several types can still collide, which the dict checks.
        if "missing" in schema or any(type(value) not in _JSON_NATIVE_TYPES for value in values):
            raise _UnsafeJsonValueError
        return ("enum", schema["cls"], frozenset(map(type, values))) if is_in_json else None
    if not is_in_json:
        return None
    if schema_type == "any":
        return ("any",)
    if schema_type == "literal":
        expected = schema["expected"]
        # As for enums, the values must stay what they are when we encode them, and differ from each other in JSON,
        # where e.g. `1` and `1.0` might be the same value
        if any(type(value) not in _JSON_NATIVE_TYPES for value in expected) or len(set(expected)) != len(expected):
            raise _UnsafeJsonValueError
        return "leaf", frozenset(map(type, expected))
    return "leaf", frozenset(_JSON_LEAF_VALUE_TYPES[schema_type])


def _walk_json_reference(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    ref = schema["schema_ref"]
    if ref not in visiting:
        return _walk_json_values(_CORE_SCHEMA_DEFINITIONS[ref], visiting | {ref}, memo, is_in_json=is_in_json)
    # Outside of `Json[...]` values, we're already describing what the reference refers to, but we don't describe
    # the `Json[...]` values that its data has deeper down, so the head would decode their strings once more
    if is_in_json or _has_core_schema_of_types(_CORE_SCHEMA_DEFINITIONS[ref], frozenset(("json",))):
        raise _UnsafeJsonValueError
    return None


def _walk_json_schema(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    # A `Json[...]` value in a `Json[...]` value would need JSON in JSON. We don't leave the `Json[...]` values that we
    # can't encode as they are, because the head would decode their strings once more if we encoded the others.
    if is_in_json:
        raise _UnsafeJsonValueError
    return "json", (
        _walk_json_values(schema["schema"], frozenset(), memo, is_in_json=True) if "schema" in schema else ("any",)
    )


def _walk_json_union(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Union[_JsonValuesPlan, None]:
    choices = schema["choices"].values() if isinstance(schema["choices"], dict) else schema["choices"]
    choices = [choice[0] if isinstance(choice, tuple) else choice for choice in choices]
    # The head might pick another member for the dump than the request's version has picked for what the client has
    # sent, e.g. for the dump of a model, a string that a constraint has changed, or a decimal or a date that we encode
    # as a string, so we only allow the members that keep their values and their JSON types
    if not all(_is_plain_json_schema(choice) for choice in choices):
        raise _UnsafeJsonValueError
    if not is_in_json:
        return None
    # In `Json[...]` values, we check the types of the values of the members that are plain values
    types: set[type] = set()
    for choice in choices:
        check = _walk_json_values(choice, visiting, memo, is_in_json=True)
        if check[0] == "any":
            return check
        if check[0] != "leaf":
            raise _UnsafeJsonValueError
        types.update(check[1])
    return "leaf", frozenset(types)


def _walk_json_tuple(
    schema: dict[str, Any], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> None:
    # We don't encode or check the values in tuples, so they mustn't have any
    if is_in_json or any(
        _walk_json_values(item, visiting, memo, is_in_json=False) is not None for item in schema["items_schema"]
    ):
        raise _UnsafeJsonValueError


_JSON_SCHEMA_WALKERS: dict[str, Callable[..., Any]] = {
    "definition-ref": _walk_json_reference,
    "json": _walk_json_schema,
    "union": _walk_json_union,
    "tagged-union": _walk_json_union,
    "tuple": _walk_json_tuple,
    "model": lambda schema, visiting, memo, *, is_in_json: _walk_json_model(
        schema["cls"], visiting, memo, is_in_json=is_in_json
    ),
}


def _is_plain_json_schema(schema: dict[str, Any]) -> bool:
    """Whether the core schema only has values that keep their JSON type when we encode them"""
    stack = [schema]
    while stack:
        node = stack.pop()
        # Constraints (e.g. `pattern`), transformations (e.g. `to_upper`), serializers, and strictness change what
        # the members accept
        if (
            node["type"] not in _PLAIN_JSON_SCHEMA_TYPES
            or not node.keys() <= _PLAIN_JSON_SCHEMA_KEYS
            or (
                node["type"] == "literal" and any(type(value) not in _PLAIN_LITERAL_TYPES for value in node["expected"])
            )
        ):
            return False
        stack.extend(node[key] for key in ("schema", "items_schema", "keys_schema", "values_schema") if key in node)
    return True


def _walk_json_model(
    model: type[BaseModel], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    depth = sum(isinstance(item, type) for item in visiting)
    # We can't check the dumps of the models that are deeper than that. A model that we're already describing refers
    # to itself, e.g. through a model that it refers to, and we don't describe the data deeper down.
    if model in visiting or depth >= _MAX_JSON_PLAN_DEPTH:
        raise _UnsafeJsonValueError
    # A model can be in many places, so we only describe it once for each depth
    key = (is_in_json, model, depth)
    if key not in memo:
        try:
            memo[key] = _describe_json_model(model, visiting | {model}, memo, is_in_json=is_in_json)
        except _UnsafeJsonValueError:
            memo[key] = _UNSAFE_JSON_VALUE
    if memo[key] is _UNSAFE_JSON_VALUE:
        raise _UnsafeJsonValueError
    return memo[key]


def _get_unchanging_model_core_schema(model: type[BaseModel]) -> Union[dict[str, Any], None]:
    """Return the fields core schema (or the root core schema) of a model that keeps its values as they are, or None.

    Besides what `_get_model_inner_core_schema` rejects, its validators of any kind and `model_post_init` could
    change the values: they wrap the model schema or its fields schema, or are its "post_init".
    """
    inner_schema = _get_model_inner_core_schema(model)
    if inner_schema is None:
        return None
    schema = _register_core_schema_definitions(cast("dict[str, Any]", model.__pydantic_core_schema__))
    if schema["type"] == "definition-ref":
        schema = _CORE_SCHEMA_DEFINITIONS[schema["schema_ref"]]
    # `_get_model_inner_core_schema` unwraps the "after" validators of the model and of its fields schema, so we check
    # that the model schema is the outermost one and that its fields schema is its own schema
    if schema["type"] != "model" or schema.get("post_init") or schema["schema"] is not inner_schema:
        return None
    return inner_schema


def _get_dumped_key(name: str, field: FieldInfo) -> str:
    # An empty alias is an alias too
    return name if field.serialization_alias is None else field.serialization_alias


def _describe_json_model(
    model: type[BaseModel], visiting: frozenset[object], memo: _JsonPlanMemo, *, is_in_json: bool
) -> Any:
    """Describe the data of a model whose every field the head reads from the key that the field is dumped to.

    Otherwise, the head would give a field its default instead of the client's value, or the value of another field,
    so we raise `_UnsafeJsonValueError`.
    """
    inner_schema = _get_unchanging_model_core_schema(model)
    # Models that allow extra keys dump the keys that they don't read, which can overwrite the dumps of their fields,
    # and the dump computes the computed fields once more, which might give other values (e.g. of a counter)
    if inner_schema is None or _get_validation_settings(model)[2] == "allow" or model.model_computed_fields:
        raise _UnsafeJsonValueError
    # The settings of the model apply to the members of its unions too
    if any(model.model_config.get(key) for key in _STRING_CONFIG_KEYS) and _has_core_schema_of_types(
        inner_schema, frozenset(("union", "tagged-union"))
    ):
        raise _UnsafeJsonValueError
    if issubclass(model, RootModel):
        return _walk_json_values(inner_schema, visiting, memo, is_in_json=is_in_json)
    dumped_keys = [_get_dumped_key(name, field) for name, field in model.model_fields.items()]
    keys = frozenset(dumped_keys)
    if len(keys) != len(dumped_keys):
        raise _UnsafeJsonValueError
    children = []
    for (name, field), dumped_key in zip(model.model_fields.items(), dumped_keys, strict=True):
        field_schema = inner_schema["fields"][name]
        paths = _get_validation_paths(model, name, field)
        # The head must read every field from its dumped key and not from a dumped key of anything else
        if (
            not _do_compiled_aliases_match(field, field_schema)
            or field_schema.get("serialization_exclude")
            or "serialization_exclude_if" in field_schema
            or [dumped_key] not in paths
            or any(path != [dumped_key] for path in paths if path[0] in keys)
        ):
            raise _UnsafeJsonValueError
        child = _walk_json_values(field_schema["schema"], visiting, memo, is_in_json=is_in_json)
        if child is not None:
            children.append((dumped_key, child))
    return "model", keys, tuple(children)


def _has_json_values_to_encode(
    plan: _JsonValuesPlan,
    data: Any,
    is_to_encode: Callable[[_JsonValuesPlan, Any, "_JsonPath"], bool],
    path: "_JsonPath",
) -> bool:
    """Whether the data has a value to encode, found without changing anything or running code of users.

    It doesn't copy, hash, or compare anything of users (e.g. keys). It walks all of the data, because `is_to_encode`
    raises `_UnsafeJsonValueError` for a value that we can't encode, and it raises for containers that we can't walk
    into too.
    """
    kind = plan[0]
    if kind == "json":
        return is_to_encode(plan[1], data, path)
    if kind == "nullable":
        return data is not None and _has_json_values_to_encode(plan[1], data, is_to_encode, path)
    _is_json_container(kind, data, is_strict=True)
    if kind == "items":
        children = [(plan[1], item, (*path, index)) for index, item in enumerate(data)]
    elif kind == "values":
        children = [(plan[1], item, (*path, _get_json_path_key(key))) for key, item in data.items()]
    else:
        children = [(child, data[key], (*path, key)) for key, child in plan[2] if key in data]
    # We check every child before `any()` stops at the first value to encode
    results = [_has_json_values_to_encode(child, item, is_to_encode, item_path) for child, item, item_path in children]
    return any(results)


def _replace_planned_json_values(
    plan: _JsonValuesPlan,
    data: Any,
    replace: Callable[[_JsonValuesPlan, Any, "_JsonPath"], Any],
    path: "_JsonPath",
    *,
    is_strict: bool,
) -> Any:
    """Replace the values of the plan in the data, copying only the containers in which something changes.

    Checking the dependencies runs code of users (e.g. schema hooks) after `_has_json_values_to_encode`, which might
    change the data, so if `is_strict`, we check the data again as we copy it, and raise `_UnsafeJsonValueError` like
    it does. Otherwise, we skip the containers that we can't walk into.
    """
    kind = plan[0]
    if kind == "json":
        return replace(plan[1], data, path)
    if kind == "nullable":
        return data if data is None else _replace_planned_json_values(plan[1], data, replace, path, is_strict=is_strict)
    if not _is_json_container(kind, data, is_strict=is_strict):
        return data
    if kind == "items":
        items = [
            _replace_planned_json_values(plan[1], item, replace, (*path, index), is_strict=is_strict)
            for index, item in enumerate(data)
        ]
        return data if all(new is old for new, old in zip(items, data, strict=True)) else items
    if kind == "values":
        values = {
            key: _replace_planned_json_values(
                plan[1], value, replace, (*path, _get_json_path_key(key)), is_strict=is_strict
            )
            for key, value in data.items()
        }
        return data if all(values[key] is value for key, value in data.items()) else values
    replaced = data
    for key, field_plan in plan[2]:
        if (
            key in data
            and (
                value := _replace_planned_json_values(field_plan, data[key], replace, (*path, key), is_strict=is_strict)
            )
            is not data[key]
        ):
            if replaced is data:
                replaced = dict(data)
            replaced[key] = value
    return replaced


def _get_json_path_key(key: Any) -> tuple[type, Any]:
    # Keys of different types can be equal, e.g. `1` and `True`, but aren't the same key in JSON
    return type(key), key


def _is_json_value_to_encode(
    check: _JsonValuesPlan, value: Any, path: "_JsonPath", *, strings: "dict[_JsonPath, str]"
) -> bool:
    """Whether to encode the value of a `Json[...]` field, or raise `_UnsafeJsonValueError` if we can't encode it"""
    # None stays as it is because FastAPI before 0.151.0 gives a body that is None its default
    if value is None:
        return False
    # Any other string might be JSON that a migration has encoded already
    if (type(value) is str and not _is_client_json_string(value, path, strings)) or not _passes_json_value_check(
        check, value
    ):
        raise _UnsafeJsonValueError
    return True


def _is_client_json_string(value: Any, path: "_JsonPath", strings: "dict[_JsonPath, str]") -> bool:
    """Whether the value is the copy of a string that the client has sent, which we have put at the path"""
    # Paths only have the keys of dicts that `_is_json_container` has checked, so hashing them runs no code of users
    return type(value) is str and strings.get(path) is value


class _ClientJsonStringsCheck:
    """Says whether a `Json[...]` value is a string that the client has sent, as `_is_client_json_string` does.

    It raises `_UnsafeJsonValueError` if a string that the client has sent is at the place of another value, and
    `check_that_all_are_in_place()` raises it if one of their places isn't in the body anymore, e.g. because
    a migration has moved or removed it. A place that has another value, e.g. JSON that a migration has encoded, is
    fine. The check must see every `Json[...]` value of the body, so walk it strictly.
    """

    def __init__(self, strings: "dict[_JsonPath, str]") -> None:
        self.strings = strings
        self.strings_by_id = {id(string): string for string in strings.values()}
        self.seen_paths: set[_JsonPath] = set()

    def __call__(self, _check: _JsonValuesPlan, value: Any, path: "_JsonPath") -> bool:
        if path in self.strings:
            self.seen_paths.add(path)
        is_client_string = _is_client_json_string(value, path, self.strings)
        if not is_client_string and self.strings_by_id.get(id(value)) is value:
            raise _UnsafeJsonValueError
        return is_client_string

    def check_that_all_are_in_place(self) -> None:
        if len(self.seen_paths) != len(self.strings):
            raise _UnsafeJsonValueError


def _is_json_container(kind: str, data: Any, *, is_strict: bool) -> bool:
    """Whether the data is the container of the kind, as dumps have them, or raise `_UnsafeJsonValueError` if strict.

    Dumps only have lists and dicts. We don't accept other containers, even subclasses (e.g. a tuple or a mapping that
    a migration has put in the data), whose `Json[...]` values we might not encode. Looking keys up and copying dicts
    hashes and compares the keys, which runs the code of users (e.g. the `__eq__` of a subclass of `str` that
    a migration has set, or the `__hash__` of an enum). So the keys of models must be exactly strings, and the keys of
    other dicts exactly the values that JSON has. Iterating over the keys runs nothing of users.
    """
    key_types = (str,) if kind == "model" else _JSON_NATIVE_TYPES
    if kind == "items":
        is_container = type(data) is list
    else:
        is_container = type(data) is dict and all(type(key) in key_types for key in data)
    if not is_container and is_strict:
        raise _UnsafeJsonValueError
    return is_container


def _encode_json_value(value: Any) -> str:
    """Return the value encoded as JSON, or raise `_UnsafeJsonValueError` if we can't encode it"""
    try:
        return to_json(value).decode()
    # E.g. a string with a lone surrogate, which a migration can set
    except PydanticSerializationError as e:
        raise _UnsafeJsonValueError from e


def _is_json_value(value: Any) -> bool:
    """Whether the value only has the values that JSON has, so that it stays what it is when we encode it.

    A container that we've seen already stops the walk, e.g. a list that a migration has put into itself.
    """
    stack = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if type(item) in (dict, list):
            if id(item) in seen:
                return False
            seen.add(id(item))
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                return False
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
        elif type(item) not in _JSON_NATIVE_TYPES or _is_nan(item):
            return False
    return True


def _is_nan(value: Any) -> bool:
    # JSON has no sign for NaN, so a negative NaN would become a positive one
    return type(value) is float and value != value


def _is_json_temporal(value: Union[datetime, time]) -> bool:
    """Whether JSON has the datetime or time, e.g. its offset has no seconds, which JSON offsets don't have.

    JSON gives values the time zones of pydantic and no folds, so we don't encode other ones either.
    """
    if value.fold or (value.tzinfo is not None and type(value.tzinfo) is not TzInfo):
        return False
    offset = value.utcoffset()
    return offset is None or offset % timedelta(minutes=1) == timedelta(0)


def _are_json_keys(keys_check: _JsonValuesPlan, value: dict[Any, Any]) -> bool:
    """Whether the keys of the dict pass the check and stay different keys in JSON, whose keys are strings"""
    # Keys that the schema doesn't type must be strings, which stay the keys that they are
    if keys_check[0] == "any":
        return all(type(key) is str for key in value)
    if not all(_passes_json_value_check(keys_check, key) for key in value):
        return False
    try:
        return len({to_json(key) for key in value}) == len(value)
    except PydanticSerializationError:
        return False


def _passes_json_value_check(check: _JsonValuesPlan, value: Any) -> bool:
    """Whether the head reads all of the value, e.g. a migration hasn't left a key that the head doesn't read"""
    kind = check[0]
    if kind == "any":
        return _is_json_value(value)
    if kind == "leaf":
        return (
            type(value) in check[1]
            and not _is_nan(value)
            and (not isinstance(value, (datetime, time)) or _is_json_temporal(value))
        )
    if kind == "enum":
        # A member of another enum might have a value of a member of this one. Dumps have members, because the walk
        # stops at `use_enum_values`, so other values (e.g. that a migration sets) stop the body too.
        return type(value) is check[1] and not _is_nan(value.value)
    if kind == "nullable":
        return value is None or _passes_json_value_check(check[1], value)
    if kind == "items":
        return type(value) is list and all(_passes_json_value_check(check[1], item) for item in value)
    if kind == "values":
        _, values_check, keys_check = check
        return (
            type(value) is dict
            and _are_json_keys(keys_check, value)
            and all(_passes_json_value_check(values_check, item) for item in value.values())
        )
    _, keys, checks = check
    # Comparing and looking keys up runs the `__eq__` and `__hash__` of subclasses of `str`, iterating doesn't
    return (
        type(value) is dict
        and all(type(key) is str for key in value)
        and value.keys() <= keys
        and all(key not in value or _passes_json_value_check(key_check, value[key]) for key, key_check in checks)
    )


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


def _replace_with_head_enum_members(data: Any) -> Any:
    """Replace each member of a copy of a plain enum or flag with the member of its head enum that has the same value.

    Python-mode dumps keep the members of the copies, which the head rejects for plain enums and flags. The head looks
    up the members of the copies of `str`, `int`, and `float` enums by their values anyway, so we leave them, as well
    as the members of other enums and the ones that their head enums have no member with exactly the same value for,
    so that the head rejects them as before.

    We only walk into the containers that pydantic makes when it validates a field, and copy only the ones in which
    something below them changes, with the same types. Copying them again hashes their keys and items, which runs code
    of users, so if a copy fails, we keep the container as it is. Anything else, such as models (a dump has none, but
    a migration can put some into the body), iterators, or containers of other types, could run code of users or lose
    what they hold if we iterated or copied it, so we leave it as it is. We walk depth-first without recursion, so that
    data as deep as pydantic accepts never hits the recursion limit, and we don't walk into the containers that contain
    themselves again.
    """
    children = _get_children_that_can_be_enum_members(data)
    if children is None:
        return _replace_with_head_enum_member(data)
    # Each frame is [container, iterator over its children, its children with replaced members, whether any changed]
    stack: list[list[Any]] = [[data, iter(children), [], False]]
    containers_on_stack = {id(data)}
    while True:
        frame = stack[-1]
        child = next(frame[1], _NO_CHILD)
        if child is not _NO_CHILD:
            grandchildren = None if id(child) in containers_on_stack else _get_children_that_can_be_enum_members(child)
            if grandchildren is None:
                replaced_child = _replace_with_head_enum_member(child)
                frame[2].append(replaced_child)
                frame[3] = frame[3] or replaced_child is not child
            else:
                stack.append([child, iter(grandchildren), [], False])
                containers_on_stack.add(id(child))
            continue
        stack.pop()
        container = frame[0]
        containers_on_stack.discard(id(container))
        replaced = _copy_container_with_children(container, frame[2]) if frame[3] else container
        if not stack:
            return replaced
        stack[-1][2].append(replaced)
        stack[-1][3] = stack[-1][3] or replaced is not container


_NO_CHILD = object()
# The containers that pydantic makes when it validates a field (see `GenerateSchema` and `MAPPING_ORIGIN_MAP`)
_MAPPINGS_THAT_PYDANTIC_MAKES = (dict, OrderedDict, defaultdict, Counter)
_COLLECTIONS_THAT_PYDANTIC_MAKES = (list, tuple, set, frozenset, deque)
# Pydantic doesn't export these iterator classes. Obtain their exact types through its public core schema API,
# rather than matching class names or wrapping user iterators (including ones that migrations put into the body).
_PYDANTIC_ITERATOR_TYPES = (
    type(SchemaValidator(core_schema.generator_schema()).validate_python(())),
    type(SchemaSerializer(core_schema.generator_schema()).to_python(iter(()))),
)


def _replace_with_head_enum_member(value: Any) -> Any:
    if not isinstance(value, Enum):
        return value
    head_enum: Union[type[Enum], None] = getattr(type(value), "__cadwyn_original_model__", None)
    if head_enum is None or is_enum_looked_up_by_value(head_enum):
        return value
    head_member = get_member_with_same_value(head_enum, value)
    return value if head_member is None else head_member


def _to_head_lazy_enum_input(
    body: Any, head_dependant: Dependant, head_route: APIRoute, *, enabled: bool, embed_body_fields: bool
) -> Any:
    """Convert lazy members only where every body reader expects the same plain enum.

    An enum inside a union can already succeed through another branch. Walking into that union, or into opaque
    validators or Any, would change working requests. Leave these paths and user iterators as they are.
    """
    if (
        not enabled
        or embed_body_fields
        or len(head_dependant.body_params) != 1
        or _get_dependency_overrides(head_route)
    ):
        return body
    plan = _get_lazy_enum_field_plan(head_dependant.body_params[0])
    if plan is None or any(
        _get_lazy_enum_field_plan(field) != plan
        for dependant in _iter_dependants_that_fastapi_solves(head_dependant, head_route)
        for field in dependant.body_params
    ):
        return body
    try:
        return _to_lazy_enum_input_by_plan(body, plan)
    except RecursionError:  # pragma: no cover # Leave bodies deeper than Python's recursion limit unchanged.
        return body


_LAZY_ENUM_INPUT_PLANS: dict[int, tuple[ModelField, _InputPlan]] = {}


def _get_lazy_enum_field_plan(field: ModelField) -> _InputPlan:
    cached = _LAZY_ENUM_INPUT_PLANS.get(id(field))
    if cached is not None and cached[0] is field:
        return cached[1]
    try:
        plan = _get_lazy_enum_input_plan(cast("dict[str, Any]", field._type_adapter.core_schema))
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas.
        plan = None
    _LAZY_ENUM_INPUT_PLANS[id(field)] = field, plan
    return plan


def _get_lazy_enum_input_plan(
    schema: dict[str, Any], *, lazy_item: bool = False, visiting: frozenset[str] = frozenset()
) -> _InputPlan:
    schema = _register_core_schema_definitions(schema)
    kind = schema["type"]
    if kind == "definition-ref":
        ref = schema["schema_ref"]
        if ref in visiting:
            return None
        return _get_lazy_enum_input_plan(_CORE_SCHEMA_DEFINITIONS[ref], lazy_item=lazy_item, visiting=visiting | {ref})
    if kind == "nullable" or (kind == "default" and schema.get("on_error", "raise") == "raise"):
        return _get_lazy_enum_input_plan(schema["schema"], lazy_item=lazy_item, visiting=visiting)
    # Item after-validators only run after the enum accepts the item. A validator surrounding a generator can
    # consume it and catch its errors, so normalization there could change a request that already succeeds.
    if kind == "function-after" and lazy_item and not _has_core_schema_of_types(schema, frozenset(("generator",))):
        return _get_lazy_enum_input_plan(schema["schema"], lazy_item=True, visiting=visiting)
    if kind == "enum" and lazy_item:
        cls = schema["cls"]
        if (
            is_enum_looked_up_by_value(cls)
            or cls._missing_.__func__ not in (Enum._missing_.__func__, Flag._missing_.__func__)
            or cls.__eq__ is not Enum.__eq__
            or cls.__hash__ is not Enum.__hash__
            or any(type(member._value_) not in (str, int, float, bool, type(None)) for member in cls)
        ):
            return None
        return "enum", cls
    if kind in ("generator", "list", "dict"):
        child_key = "values_schema" if kind == "dict" else "items_schema"
        child = _get_lazy_enum_input_plan(
            schema.get(child_key, {"type": "any"}), lazy_item=lazy_item or kind == "generator", visiting=visiting
        )
        return (kind, child) if child is not None else None
    if (
        kind == "model"
        and not schema.get("custom_init")
        and not schema.get("post_init")
        and _do_compiled_settings_match(schema["cls"], schema.get("config"))
    ):
        inner = schema["schema"]
        if schema.get("root_model"):
            return _get_lazy_enum_input_plan(inner, lazy_item=lazy_item, visiting=visiting)
        return _get_lazy_enum_model_plan(schema["cls"], inner, lazy_item=lazy_item, visiting=visiting)
    return None


def _get_lazy_enum_model_plan(
    cls: type[BaseModel], inner: dict[str, Any], *, lazy_item: bool, visiting: frozenset[str]
) -> _InputPlan:
    if inner["type"] != "model-fields" or not _do_compiled_fields_match(cls, inner["fields"]):
        return None
    fields = []
    keys_of_fields = []
    for name, field in cls.model_fields.items():
        paths = _get_validation_paths(cls, name, field)
        if any(len(path) != 1 or type(path[0]) is not str for path in paths):
            return None
        keys = tuple(path[0] for path in paths)
        keys_of_fields.append(set(keys))
        child = _get_lazy_enum_input_plan(inner["fields"][name]["schema"], lazy_item=lazy_item, visiting=visiting)
        if child is not None:
            fields.append((keys, child))
    if any(keys & other for index, keys in enumerate(keys_of_fields) for other in keys_of_fields[index + 1 :]):
        return None
    return ("fields", tuple(fields)) if fields else None


def _to_lazy_enum_input_by_plan(data: Any, plan: tuple[Any, ...]) -> Any:
    kind = plan[0]
    if kind == "enum":
        return (
            _replace_with_head_enum_member(data)
            if getattr(type(data), "__cadwyn_original_model__", None) is plan[1]
            else data
        )
    if kind == "generator":
        return (
            (_to_lazy_enum_input_by_plan(item, plan[1]) for item in data)
            if type(data) in _PYDANTIC_ITERATOR_TYPES
            else data
        )
    if kind == "list" and type(data) is list:
        children = [_to_lazy_enum_input_by_plan(item, plan[1]) for item in data]
        return children if any(old is not new for old, new in zip(data, children, strict=True)) else data
    if kind in ("dict", "fields") and type(data) is dict and all(type(key) is str for key in data):
        copied = data.copy()
        if kind == "dict":
            for key, value in data.items():
                copied[key] = _to_lazy_enum_input_by_plan(value, plan[1])
        else:
            for keys, child in plan[1]:
                for key in keys:
                    if key in data:
                        copied[key] = _to_lazy_enum_input_by_plan(data[key], child)
                        break
        return copied if any(data[key] is not copied[key] for key in data) else data
    return data


def _get_children_that_can_be_enum_members(value: Any) -> Union[list[Any], None]:
    """Return the children of a container, with the keys and values of mappings in turn, or None"""
    if type(value) in _MAPPINGS_THAT_PYDANTIC_MAKES:
        # Unlike the type's own `items` (and iterating an `OrderedDict`), `dict.items` never hashes the keys again
        return [child for item in dict.items(value) for child in item]
    if type(value) in _COLLECTIONS_THAT_PYDANTIC_MAKES:
        return list(value)
    return None


def _copy_container_with_children(container: Any, children: list[Any]) -> Any:
    """Return a copy of the container that has the children instead of its own, or the container if they don't fit"""
    try:
        if type(container) in _MAPPINGS_THAT_PYDANTIC_MAKES:
            items = list(zip(children[::2], children[1::2], strict=True))
            if type(container) is defaultdict:
                copied = defaultdict(container.default_factory, items)
            elif type(container) is Counter:
                copied = Counter(dict(items))
            elif type(container) is OrderedDict:
                # The children have the order of the dict, while `move_to_end` only changes the order of the
                # `OrderedDict`. Iterating it hashes the keys, so we only do it here, where a copy hashes them anyway.
                index_by_key_id = {id(key): index for index, key in enumerate(dict.keys(container))}
                copied = OrderedDict(items[index_by_key_id[id(key)]] for key in container)
            else:
                copied = dict(items)
        elif type(container) is deque:
            copied = deque(children, maxlen=container.maxlen)
        else:
            copied = type(container)(children)
    except Exception:  # noqa: BLE001 # E.g. a key whose `__hash__` fails
        return container
    # Members of the copies don't equal the head members, so two distinct keys or items can become one
    return copied if len(copied) == len(container) else container


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
            while (
                schema["type"] in ("definition-ref", "nullable", "default", "function-after")
                or _is_renamed_key_check(schema)
            ) and schema.get("on_error", "raise") == "raise":
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
    # We go by the field infos even for fields that the core schemas of their types give defaults to: such a field
    # wouldn't fail without its value, but it would silently get its default instead of the client's value
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
        while schema["type"] == "function-after" or _is_renamed_key_check(schema):
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


# The keys that the fields of a generated model lost because their aliases followed `alias`, which
# `_reject_keys_renamed_by_alias` rejects unless a field of the model reads them: `{field_name: renamed_keys}`
_KEYS_RENAMED_BY_ALIAS = "__cadwyn_keys_renamed_by_alias__"
# The checks that each model class makes, which we compute from its compiled core schema the first time it validates
_RENAMED_KEY_CHECKS = "__cadwyn_renamed_key_checks__"


def _reject_keys_renamed_by_alias(cls: type[BaseModel], data: Any, handler: Callable[[Any], Any]) -> Any:
    """Fail instead of silently giving a field its default when the data has the key that an alias instruction renamed.

    For example, `field("x").had(alias="old")` on `x: str = Field("", alias="new")` makes an older version use "old"
    instead of "new". So without migrations that rename the key, its clients that still send "new" and the responses
    that the latest version returns with "new" would lose the value of the field. We only look at the data as it
    comes in: it fails if it has the key that the field lost but none of the keys that the field reads now.
    """
    if isinstance(data, Mapping):
        for name, renamed_keys, keys_read_by_field in _get_renamed_key_checks(cls):
            renamed_key = next((renamed_key for renamed_key in renamed_keys if renamed_key in data), None)
            if renamed_key is not None and not any(key in data for key in keys_read_by_field):
                raise PydanticCustomError(
                    "renamed_key",
                    'The key "{renamed_key}" of "{field}" was renamed to "{key}" in this API version',
                    {"renamed_key": renamed_key, "field": f"{cls.__name__}.{name}", "key": keys_read_by_field[0]},
                )
    return handler(data)


def _get_renamed_key_checks(model: type[BaseModel]) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    """Return `(field_name, renamed_keys, keys_that_the_field_reads)` for the renamed keys that no field reads.

    The keys come from the compiled core schema of the model itself, so subclasses that change their fields and
    the settings that pydantic changes when it compiles them get their own.
    """
    checks = model.__dict__.get(_RENAMED_KEY_CHECKS)
    if checks is None:
        keys_read_by_fields = _get_keys_read_by_fields(model)
        keys_read_by_model = {key for keys in keys_read_by_fields.values() for key in keys}
        checks = tuple(
            (name, tuple(sorted(renamed_keys - keys_read_by_model)), keys_read_by_fields[name])
            for name, renamed_keys in getattr(model, _KEYS_RENAMED_BY_ALIAS).items()
            if name in keys_read_by_fields and not renamed_keys <= keys_read_by_model
        )
        setattr(model, _RENAMED_KEY_CHECKS, checks)
    return checks


def _get_keys_read_by_fields(model: type[BaseModel]) -> dict[str, tuple[str, ...]]:
    """Return the keys of the input that pydantic reads for each field, from the compiled core schema of the model"""
    schema = cast("dict[str, Any]", model.__pydantic_core_schema__)
    definitions: dict[str, dict[str, Any]] = {}
    if schema["type"] == "definitions":
        definitions = {definition["ref"]: definition for definition in schema["definitions"]}
        schema = schema["schema"]
    # Validators of the model, including `_reject_keys_renamed_by_alias`, wrap it
    while schema["type"] != "model":
        schema = definitions[schema["schema_ref"]] if schema["type"] == "definition-ref" else schema["schema"]
    config = schema.get("config", {})
    # Like pydantic-core, which reads the alias if it validates by alias, and the name if it validates by name or if
    # the field has no alias
    validate_by_alias, validate_by_name = config.get("validate_by_alias", True), config.get("validate_by_name", False)
    fields_schema = schema["schema"]
    # "before" and "after" validators of the model wrap its fields schema
    while fields_schema["type"] != "model-fields":
        fields_schema = fields_schema["schema"]
    keys_read_by_fields = {}
    for name, field_schema in fields_schema["fields"].items():
        alias = field_schema.get("validation_alias")
        paths = cast("list[list[str]]", _normalize_validation_alias(alias)) if alias is not None else []
        keys = [path[0] for path in paths] if validate_by_alias else []
        if alias is None or validate_by_name:
            keys.append(name)
        keys_read_by_fields[name] = tuple(keys)
    return keys_read_by_fields


def _is_renamed_key_check(schema: dict[str, Any]) -> bool:
    """Whether the core schema is the check that Cadwyn adds to the models whose alias instructions renamed keys.

    It only rejects some data and never changes any, so it's safe wherever the model that it wraps is.
    """
    return (
        schema["type"] == "function-wrap"
        and getattr(schema["function"]["function"], "__func__", None) is _reject_keys_renamed_by_alias
    )


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
    if _is_renamed_key_check(schema):
        return _has_unsafe_core_schema(schema["schema"], units, is_unit_body=is_unit_body)
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


def _get_hidden_background_tasks_param_name(endpoint: Callable[..., object]) -> str:
    """Return a name for the BackgroundTasks param that Cadwyn adds to the endpoint, which none of its params has."""
    taken_names = {*inspect.signature(endpoint).parameters, _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME}
    name = _CADWYN_BACKGROUND_TASKS_PARAM_NAME
    while name in taken_names:
        name += "_"
    return name


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


@dataclasses.dataclass
class _DroppedBackgroundTasksWarning:
    """Warns once per route that Cadwyn has dropped the background tasks of the latest version's dependencies."""

    route: str
    has_warned: bool = False
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    async def warn_after_the_response(self, request: FastapiRequest) -> None:
        """Warn once FastAPI has sent the response and run its background tasks, unless the request has failed.

        That's when FastAPI would have run the dropped tasks. It closes this exit stack right after.
        """
        if self.has_warned:
            return
        request_stack = request.scope.get("fastapi_inner_astack")
        if not isinstance(request_stack, AsyncExitStack):
            # Every supported FastAPI version sets it. Without it, we warn right away rather than not at all
            await run_in_threadpool(self._warn, is_deferred=False)
            return

        async def warn_unless_the_request_has_failed(exc_type: Union[type[BaseException], None], *_: object) -> None:
            if exc_type is None:
                # Warning filters and log handlers can block, so we keep them off the event loop
                await run_in_threadpool(self._warn)

        request_stack.push_async_exit(warn_unless_the_request_has_failed)

    def _warn(self, *, is_deferred: bool = True) -> None:
        message = (
            f"Cadwyn has dropped a background task of {self.route}: a dependency has added it while Cadwyn solved the "
            "dependencies of the latest version, and the endpoint doesn't declare a `BackgroundTasks` param. "
            "Cadwyn 10.0 will run such tasks after the response. To run them now, pass "
            f"`{_RUN_BACKGROUND_TASKS_FLAG}=True` to `Cadwyn()`. Dependencies run both for the request's version and "
            "for the latest version, so a task that a dependency adds in both runs will then run twice. To keep a "
            'single run, add it only when `current_dependency_solver` is "fastapi".'
        )
        if not is_deferred:
            message += (
                " Cadwyn couldn't defer this warning until FastAPI has sent the response, so it warns even if the "
                "request fails afterwards."
            )
        with self._lock:
            if self.has_warned:
                return
            # Nothing here may break the request, even if warnings are errors or logging fails
            try:
                warnings.warn(message, FutureWarning, stacklevel=1)
            except Exception:  # noqa: BLE001
                try:
                    _logger.warning(message)
                except Exception:  # noqa: BLE001
                    return
            self.has_warned = True
