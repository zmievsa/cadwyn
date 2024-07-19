import datetime
import sys
from types import NoneType
from typing import Annotated, Any, Literal, cast, get_args, get_origin

from pydantic import BaseModel, Field, RootModel

from cadwyn import (
    VersionBundle,
    VersionChangeWithSideEffects,
)
from cadwyn.schema_generation import _generate_versioned_models, _SchemaGenerator
from cadwyn.structure.versions import PossibleInstructions

from .structure.endpoints import (
    EndpointDidntExistInstruction,
    EndpointExistedInstruction,
    EndpointHadInstruction,
)
from .structure.enums import EnumDidntHaveMembersInstruction, EnumHadMembersInstruction
from .structure.schemas import (
    FieldDidntExistInstruction,
    FieldExistedAsInstruction,
    ValidatorDidntExistInstruction,
    ValidatorExistedInstruction,
)

if sys.version_info >= (3, 11):  # pragma: no cover
    from enum import StrEnum
else:  # pragma: no cover
    from backports.strenum import StrEnum
import builtins

import pydantic


def _convert_version_change_instruction_to_changelog_entry(
    instruction: PossibleInstructions,
    generator_from_newer_version: _SchemaGenerator,
):
    match instruction:
        case EndpointDidntExistInstruction():
            return CadwynEndpointWasAddedChangelogEntry(
                path=instruction.endpoint_path,
                methods=cast(Any, instruction.endpoint_methods),
            )
        case EndpointExistedInstruction():
            return CadwynEndpointWasRemovedChangelogEntry(
                path=instruction.endpoint_path,
                methods=cast(Any, instruction.endpoint_methods),
            )
        case EndpointHadInstruction():
            instruction.attributes
            return CadwynEndpointHadChangelogEntry
        case EnumDidntHaveMembersInstruction():
            raise Exception
        case EnumHadMembersInstruction():
            return CadwynEnumMembersWereRemovedChangelogEntry(
                enum=generator_from_newer_version[instruction.enum].__name__,
                members=list(instruction.members),
            )
        case FieldExistedAsInstruction():
            return CadwynSchemaFieldWasRemovedChangelogEntry(
                schema=generator_from_newer_version._get_wrapper_for_model(instruction.schema).name,
                field=instruction.name,
            )
        case FieldDidntExistInstruction():
            model = generator_from_newer_version[instruction.schema]
            annotation = model.model_fields[instruction.name].annotation

            type_ = _convert_annotation_to_openapi_type(annotation)

            return CadwynSchemaFieldWasAddedChangelogEntry(
                schema=model.__name__,
                field=instruction.name,
                field_info=CadwynFieldInfo(type=type_, nullable=isinstance(annotation, NoneType)),
            )
    raise Exception


def _convert_annotation_to_openapi_type(annotation: Any):
    if isinstance(annotation, list):
        if len(annotation) > 1:
            return None
        annotation = annotation[0]
    origin = get_origin(annotation)
    if origin is Annotated:
        return _convert_annotation_to_openapi_type(get_args(annotation)[0])
    if origin:
        return _convert_annotation_to_openapi_type(origin), get_args(annotation)

    match annotation:
        case pydantic.BaseModel:
            type_ = annotation.__name__
        case builtins.dict:
            type_ = "object"
        case builtins.list:
            type_ = "array"
        case builtins.str:
            type_ = "string"
        case builtins.bool:
            type_ = "boolean"
        case builtins.float:
            type_ = "number"
        case builtins.int:
            type_ = "integer"
        case _:
            raise Exception
    return type_


class ChangelogEntryType(StrEnum):
    endpoint_added = "endpoint.added"
    endpoint_removed = "endpoint.removed"
    enum_members_removed = "enum.members.removed"
    schema_field_removed = "schema.field.removed"
    schema_field_added = "schema.field.added"


SchemaAliasField = Annotated[str, Field(validation_alias="schema", serialization_alias="schema")]


class CadwynFieldInfo(BaseModel):
    type: str
    nullable: bool


class CadwynSchemaFieldWasAddedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.schema_field_added] = ChangelogEntryType.schema_field_added
    schema_: str = Field(alias="schema")
    # TODO: We could actually add field_info as well but I'm not sure which attributes to include yet
    field: str
    field_info: CadwynFieldInfo


class CadwynSchemaFieldWasRemovedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.schema_field_removed] = ChangelogEntryType.schema_field_removed
    schema_: str = Field(alias="schema")
    field: str


class CadwynEnumMembersWereRemovedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.enum_members_removed] = ChangelogEntryType.enum_members_removed
    enum: str
    members: list[str]


class HTTPMethod(StrEnum):
    GET = "GET"
    PUT = "PUT"
    POST = "POST"
    DELETE = "DELETE"
    OPTIONS = "OPTIONS"
    HEAD = "HEAD"
    PATCH = "PATCH"
    TRACE = "TRACE"


class CadwynEndpointHadChangelogEntry(BaseModel):
    pass


class CadwynEndpointWasAddedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.endpoint_added] = ChangelogEntryType.endpoint_added
    path: str
    methods: list[HTTPMethod]


class CadwynEndpointWasRemovedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.endpoint_removed] = ChangelogEntryType.endpoint_removed
    path: str
    methods: list[HTTPMethod]


class CadwynChangelogResource(BaseModel):
    versions: "list[CadwynVersion]" = Field(default_factory=list)


class CadwynVersion(BaseModel):
    value: datetime.date
    changes: "list[CadwynVersionChange]" = Field(default_factory=list)


class CadwynVersionChange(BaseModel):
    description: str
    side_effects: bool
    instructions: "list[CadwynVersionChangeInstruction]" = Field(default_factory=list)


CadwynVersionChangeInstruction = RootModel[
    CadwynEnumMembersWereRemovedChangelogEntry
    | CadwynEndpointWasAddedChangelogEntry
    | CadwynEndpointWasRemovedChangelogEntry
    | CadwynSchemaFieldWasRemovedChangelogEntry
    | CadwynSchemaFieldWasAddedChangelogEntry
]


def generate_changelog(versions: VersionBundle):
    changelog = CadwynChangelogResource()
    schema_generators = _generate_versioned_models(versions)
    for version in versions:
        version_changelog = CadwynVersion(value=version.value)
        generator = schema_generators[version.value.isoformat()]
        for version_change in version.changes:
            if version_change.is_hidden_from_changelog:
                continue
            version_change_changelog = CadwynVersionChange(
                description=version_change.description,
                side_effects=isinstance(version_change, VersionChangeWithSideEffects),
            )
            for instruction in [
                *version_change.alter_endpoint_instructions,
                *version_change.alter_enum_instructions,
                *version_change.alter_schema_instructions,
            ]:
                if (
                    isinstance(instruction, (ValidatorDidntExistInstruction, ValidatorExistedInstruction))
                    or instruction.is_hidden_from_changelog
                ):
                    continue
                version_change_changelog.instructions.append(
                    CadwynVersionChangeInstruction(
                        _convert_version_change_instruction_to_changelog_entry(instruction, generator)
                    )
                )
            version_changelog.changes.append(version_change_changelog)
        changelog.versions.append(version_changelog)
    return changelog
