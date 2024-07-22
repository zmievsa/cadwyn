import datetime
import sys
from types import NoneType
from typing import Annotated, Any, Literal, cast, get_args, get_origin

from fastapi._compat import (
    GenerateJsonSchema,
    JsonSchemaValue,
    ModelField,
    get_compat_model_name_map,
    get_definitions,
    get_schema_from_model_field,
)
from fastapi.openapi.constants import REF_TEMPLATE
from fastapi.openapi.utils import get_fields_from_routes
from pydantic import BaseModel, Field, RootModel
from typing_extensions import assert_never

from cadwyn import (
    VersionBundle,
    VersionChangeWithSideEffects,
)
from cadwyn._asts import GenericAliasUnion
from cadwyn._utils import Sentinel
from cadwyn.applications import Cadwyn
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
    FieldDidntHaveInstruction,
    FieldExistedAsInstruction,
    FieldHadInstruction,
    SchemaHadInstruction,
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
    schemas_from_last_version: list[ModelField],
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
            ...
        case FieldHadInstruction():

            class CadwynModifiedFieldAttribute(BaseModel):
                name: str
                old_value: Any
                new_value: Any

            model = generator_from_newer_version._get_wrapper_for_model(instruction.schema)

            modified_fields: list[CadwynModifiedFieldAttribute] = []
            if instruction.new_name is not Sentinel:
                modified_fields.append(
                    CadwynModifiedFieldAttribute(
                        name="name", old_value=instruction.new_name, new_value=instruction.name
                    )
                )
            if instruction.type is not Sentinel:
                modified_fields.append(
                    CadwynModifiedFieldAttribute(
                        name="type", old_value=instruction.type, new_value=model.fields[instruction.name].annotation
                    )
                )
            for attr_name in instruction.field_changes.__dataclass_fields__:
                attr_value = getattr(instruction.field_changes, attr_name)
                if attr_value is not Sentinel:
                    modified_fields.append(
                        CadwynModifiedFieldAttribute(
                            name=attr_name,
                            old_value=attr_value,
                            new_value=model.fields[instruction.name].passed_field_attributes[attr_name],
                        )
                    )
        case FieldDidntHaveInstruction():
            ...
        case EnumDidntHaveMembersInstruction():
            enum = generator_from_newer_version._get_wrapper_for_model(instruction.enum)

            return CadwynEnumMembersWereAddedChangelogEntry(
                enum=enum.name,
                members=[CadwynEnumMember(name=name, value=value) for name, value in enum.members.items()],
            )
        case EnumHadMembersInstruction():
            enum = generator_from_newer_version._get_wrapper_for_model(instruction.enum)

            return CadwynEnumMembersWereRemovedChangelogEntry(
                enum=enum.name,
                members=[CadwynEnumMember(name=name, value=value) for name, value in instruction.members.items()],
            )
        case SchemaHadInstruction():
            model = generator_from_newer_version._get_wrapper_for_model(instruction.schema)

            return CadwynSchemaWasChangedChangelogEntry(
                models=[instruction.name], model_info=CadwynModelInfo(name=model.name)
            )
        case FieldExistedAsInstruction():
            affected_model_names = _get_affected_model_names(
                instruction, generator_from_newer_version, schemas_from_last_version
            )
            return CadwynSchemaFieldWasRemovedChangelogEntry(models=affected_model_names, field=instruction.name)
        case FieldDidntExistInstruction():
            model = generator_from_newer_version[instruction.schema]
            affected_model_names = _get_affected_model_names(
                instruction, generator_from_newer_version, schemas_from_last_version
            )

            return CadwynSchemaFieldWasAddedChangelogEntry(
                models=affected_model_names,
                field=instruction.name,
                field_info=_get_openapi_representation_of_a_field(
                    ModelField(model.model_fields[instruction.name], instruction.name)
                ),
            )
        case staticmethod() | ValidatorDidntExistInstruction() | ValidatorExistedInstruction():
            return []
    assert_never(instruction)


def _get_affected_model_names(
    instruction: FieldExistedAsInstruction | FieldDidntExistInstruction,
    generator_from_newer_version: _SchemaGenerator,
    schemas_from_last_version: list[ModelField],
):
    changed_model = generator_from_newer_version._get_wrapper_for_model(instruction.schema)
    annotations = [model.field_info.annotation for model in schemas_from_last_version]
    basemodel_annotations: list[type[BaseModel]] = []
    for annotation in annotations:
        basemodel_annotations.extend(_get_all_pydantic_models_from_generic(annotation))
    models = set(
        generator_from_newer_version._get_wrapper_for_model(annotation) for annotation in basemodel_annotations
    )

    affected_models = [
        model
        for model in models
        if changed_model == model
        or (
            instruction.name not in model.fields
            and changed_model in (parents := model._get_parents(generator_from_newer_version.model_bundle.schemas))
            and all([instruction.name not in parent.fields for parent in parents[: parents.index(changed_model)]])
        )  # TODO: Test every part of this
    ]
    model_names = [model.name for model in affected_models]
    return model_names


def _get_all_pydantic_models_from_generic(annotation: Any) -> list[type[BaseModel]]:
    if not isinstance(annotation, GenericAliasUnion):
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return [annotation]
        else:
            return []
    sub_annotations = get_args(annotation)
    models = []

    for sub_annotation in sub_annotations:
        models.extend(_get_all_pydantic_models_from_generic(sub_annotation))

    return models


def _get_openapi_representation_of_a_field(field: ModelField):
    model_name_map = get_compat_model_name_map([field])
    schema_generator = GenerateJsonSchema(ref_template=REF_TEMPLATE)
    field_mapping, _ = get_definitions(
        fields=[field],
        schema_generator=schema_generator,
        model_name_map=model_name_map,
        separate_input_output_schemas=False,
    )
    return list(field_mapping.values())[0]


class ChangelogEntryType(StrEnum):
    endpoint_added = "endpoint.added"
    endpoint_removed = "endpoint.removed"
    enum_members_added = "enum.members.added"
    enum_members_removed = "enum.members.removed"
    schema_field_removed = "schema.field.removed"
    schema_field_added = "schema.field.added"


class CadwynModelInfo(BaseModel):
    name: str | None


class CadwynSchemaWasChangedChangelogEntry(BaseModel):
    models: list[str]
    model_info: CadwynModelInfo


class CadwynSchemaFieldWasAddedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.schema_field_added] = ChangelogEntryType.schema_field_added
    models: list[str]
    # TODO: We could actually add field_info as well but I'm not sure which attributes to include yet
    field: str
    field_info: dict[str, Any]


class CadwynSchemaFieldWasRemovedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.schema_field_removed] = ChangelogEntryType.schema_field_removed
    models: list[str]
    field: str


class CadwynEnumMember(BaseModel):
    name: str
    value: Any


class CadwynEnumMembersWereAddedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.schema_field_added] = ChangelogEntryType.schema_field_added
    enum: str
    members: list[CadwynEnumMember]


class CadwynEnumMembersWereRemovedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.enum_members_removed] = ChangelogEntryType.enum_members_removed
    enum: str
    members: list[CadwynEnumMember]


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


def generate_changelog(app: Cadwyn):
    changelog = CadwynChangelogResource()
    schema_generators = _generate_versioned_models(app.versions)
    for version, last_version in zip(app.versions, app.versions.versions[1:], strict=False):
        # TODO: in case of BaseUser, only list the resulting schemas in changelog instead of their parent
        schemas_from_last_version = get_fields_from_routes(app.router.versioned_routers[last_version.value].routes)
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
                changelog_entry = _convert_version_change_instruction_to_changelog_entry(
                    instruction, generator, schemas_from_last_version
                )
                if changelog_entry:
                    version_change_changelog.instructions.append(CadwynVersionChangeInstruction(changelog_entry))
            version_changelog.changes.append(version_change_changelog)
        changelog.versions.append(version_changelog)
    return changelog
