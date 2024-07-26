import datetime
import sys
from enum import auto
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
from fastapi.applications import get_openapi
from fastapi.openapi.constants import REF_TEMPLATE
from fastapi.openapi.utils import get_fields_from_routes, get_openapi_operation_parameters
from fastapi.params import Param
from pydantic import BaseModel, Field, RootModel
from pydantic.fields import FieldInfo
from typing_extensions import assert_never

from cadwyn import (
    VersionBundle,
    VersionChangeWithSideEffects,
)
from cadwyn._asts import GenericAliasUnion
from cadwyn._utils import Sentinel
from cadwyn.applications import Cadwyn
from cadwyn.schema_generation import SchemaGenerator, generate_versioned_models
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


def _convert_version_change_instruction_to_changelog_entry(
    instruction: PossibleInstructions,
    generator_from_newer_version: SchemaGenerator,
    generator_from_older_version: SchemaGenerator,
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
            path: str
            response_model: Any
            status_code: int
            tags: list[str | Enum]
            summary: str
            description: str
            response_description: str
            responses: dict[int | str, dict[str, Any]]
            deprecated: bool
            methods: list[str]
            operation_id: str
            include_in_schema: bool
            name: str

        case FieldHadInstruction():
            old_field_name = _get_older_field_name(instruction.schema, instruction.name, generator_from_older_version)

            if instruction.new_name is not Sentinel:
                attribute_changes = [
                    CadwynAttributeChange(
                        name="name",
                        status=CadwynAttributeChangeStatus.changed,
                        old_value=old_field_name,
                        new_value=instruction.name,
                    )
                ]
            else:
                attribute_changes = []

            newer_model = generator_from_newer_version[instruction.schema]
            older_model = generator_from_older_version[instruction.schema]

            newer_field_openapi = _get_openapi_representation_of_a_field(newer_model, instruction.name)
            older_field_openapi = _get_openapi_representation_of_a_field(older_model, old_field_name)

            attribute_changes += [
                CadwynAttributeChange(
                    name=key,
                    status=CadwynAttributeChangeStatus.changed
                    if key in newer_field_openapi
                    else CadwynAttributeChangeStatus.removed,
                    old_value=old_value,
                    new_value=newer_field_openapi.get(key),
                )
                for key, old_value in older_field_openapi.items()
                if old_value != newer_field_openapi.get(key)
            ]

            return CadwynFieldAttributesWereChangedChangelogEntry(
                models=_get_affected_model_names(instruction, generator_from_newer_version, schemas_from_last_version),
                field=old_field_name,
                attribute_changes=attribute_changes,
            )

        case FieldDidntHaveInstruction():
            newer_model = generator_from_newer_version[instruction.schema]
            older_model = generator_from_older_version[instruction.schema]
            newer_field_openapi = _get_openapi_representation_of_a_field(newer_model, instruction.name)
            older_field_openapi = _get_openapi_representation_of_a_field(older_model, old_field_name)

            _get_openapi_representation_of_a_field(model, instruction.name)
        case EnumDidntHaveMembersInstruction():
            enum = generator_from_newer_version._get_wrapper_for_model(instruction.enum)

            return CadwynEnumMembersWereAddedChangelogEntry(
                enum=enum.name,
                members=[CadwynEnumMember(name=name, value=value) for name, value in enum.members.items()],
            )
        case EnumHadMembersInstruction():
            new_enum = generator_from_newer_version[instruction.enum]
            old_enum = generator_from_older_version[instruction.enum]

            return CadwynEnumMembersWereChangedChangelogEntry(
                enum=new_enum.__name__,
                member_changes=[
                    CadwynAttributeChange(
                        name=name,
                        old_value=old_enum.__members__.get(name),
                        new_value=new_enum.__members__.get(name),
                        status=CadwynAttributeChangeStatus.changed
                        if name in new_enum.__members__
                        else CadwynAttributeChangeStatus.removed,
                    )
                    for name in instruction.members
                ],
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
                field_info=_get_openapi_representation_of_a_field(model, instruction.name),
            )
        case staticmethod() | ValidatorDidntExistInstruction() | ValidatorExistedInstruction():
            return []
    assert_never(instruction)


def _get_older_field_name(
    schema: type[BaseModel], new_field_name: str, generator_from_older_version: SchemaGenerator
) -> str:
    older_model_wrapper = generator_from_older_version._get_wrapper_for_model(schema)
    newer_names_mapping = {
        field.name_from_newer_version: old_name for old_name, field in older_model_wrapper.fields.items()
    }
    old_field_name = newer_names_mapping[new_field_name]
    return old_field_name


def _get_affected_model_names(
    instruction: FieldExistedAsInstruction | FieldDidntExistInstruction | FieldHadInstruction,
    generator_from_newer_version: SchemaGenerator,
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
    if this_was_tested := False:
        "Please, don't forget to test the comment above"
        hello = world
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


def _get_openapi_representation_of_a_field(model: type[BaseModel], field_name: str) -> dict:
    class CadwynDummyModelForRepresentation(BaseModel):
        my_field: model

    model_name_map = get_compat_model_name_map([CadwynDummyModelForRepresentation.model_fields["my_field"]])
    schema_generator = GenerateJsonSchema(ref_template=REF_TEMPLATE)
    field_mapping, definitions = get_definitions(
        fields=[ModelField(CadwynDummyModelForRepresentation.model_fields["my_field"], "my_field")],
        schema_generator=schema_generator,
        model_name_map=model_name_map,
        separate_input_output_schemas=False,
    )

    return definitions[model.__name__]["properties"][field_name]


class ChangelogEntryType(StrEnum):
    endpoint_added = "endpoint.added"
    endpoint_removed = "endpoint.removed"
    enum_members_added = "enum.members.added"
    enum_members_removed = "enum.members.removed"
    schema_field_removed = "schema.field.removed"
    schema_field_added = "schema.field.added"
    schema_field_attributes_changed = "schema.field.attributes.changed"
    schema_field_attributes_added = "schema.field.attributes.added"


class CadwynAttributeChangeStatus(StrEnum):
    changed = auto()
    removed = auto()


class CadwynAttributeChange(BaseModel):
    name: str
    status: CadwynAttributeChangeStatus
    old_value: Any
    new_value: Any


class CadwynFieldAttributesWereChangedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.schema_field_attributes_changed] = (
        ChangelogEntryType.schema_field_attributes_changed
    )
    models: list[str]
    field: str
    attribute_changes: list[CadwynAttributeChange]


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
    type: Literal[ChangelogEntryType.enum_members_added] = ChangelogEntryType.enum_members_added
    enum: str
    members: list[CadwynEnumMember]


class CadwynEnumMembersWereChangedChangelogEntry(BaseModel):
    type: Literal[ChangelogEntryType.enum_members_removed] = ChangelogEntryType.enum_members_removed
    enum: str
    member_changes: list[CadwynAttributeChange]


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
    CadwynEnumMembersWereAddedChangelogEntry
    | CadwynEnumMembersWereChangedChangelogEntry
    | CadwynEndpointWasAddedChangelogEntry
    | CadwynEndpointWasRemovedChangelogEntry
    | CadwynSchemaFieldWasRemovedChangelogEntry
    | CadwynSchemaFieldWasAddedChangelogEntry
    | CadwynFieldAttributesWereChangedChangelogEntry
]


def generate_changelog(app: Cadwyn):
    changelog = CadwynChangelogResource()
    schema_generators = generate_versioned_models(app.versions)
    for version, last_version in zip(app.versions, app.versions.versions[1:], strict=False):
        # TODO: in case of BaseUser, only list the resulting schemas in changelog instead of their parent
        schemas_from_last_version = get_fields_from_routes(app.router.versioned_routers[last_version.value].routes)
        version_changelog = CadwynVersion(value=version.value)
        generator_from_newer_version = schema_generators[version.value.isoformat()]
        generator_from_older_version = schema_generators[last_version.value.isoformat()]
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
                    instruction,
                    generator_from_newer_version,
                    generator_from_older_version,
                    schemas_from_last_version,
                )
                if changelog_entry:
                    version_change_changelog.instructions.append(CadwynVersionChangeInstruction(changelog_entry))
            version_changelog.changes.append(version_change_changelog)
        changelog.versions.append(version_changelog)
    return changelog
