from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cadwyn import VersionedAPIRouter, schema
from cadwyn.exceptions import CadwynStructureError, InvalidGenerationInstructionError
from tests.conftest import CreateRuntimeSchemas, CreateVersionedClients, version_change


class ConfiguredModel(BaseModel):
    model_config = ConfigDict(extra="allow", title="Current title")
    value: str


def test__schema_had_config__patch__should_preserve_other_keys_and_newer_versions(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid"))))
    older = schemas["2000-01-01"][ConfiguredModel]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        older.model_validate({"value": "x", "unknown": 1})
    assert older.model_json_schema()["title"] == "Current title"
    assert schemas["2001-01-01"][ConfiguredModel].model_validate({"value": "x", "unknown": 1}).model_dump() == {
        "value": "x",
        "unknown": 1,
    }
    assert ConfiguredModel.model_config == ConfigDict(extra="allow", title="Current title")


def test__schema_had_config__whitespace__should_transform_only_older_inputs(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(ConfiguredModel).had(config=ConfigDict(str_strip_whitespace=True)))
    )
    assert schemas["2000-01-01"][ConfiguredModel].model_validate({"value": " x "}).value == "x"
    assert schemas["2001-01-01"][ConfiguredModel].model_validate({"value": " x "}).value == " x "


def test__schema_had_config__frozen__should_reject_assignment_only_in_older_version(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(ConfiguredModel).had(config=ConfigDict(frozen=True))))
    with pytest.raises(ValidationError, match="frozen_instance"):
        schemas["2000-01-01"][ConfiguredModel](value="x").value = "y"
    latest = schemas["2001-01-01"][ConfiguredModel](value="x")
    latest.value = "y"
    assert latest.value == "y"


def test__schema_had_config__title_and_json_schema_extra__should_change_older_documentation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(
            schema(ConfiguredModel).had(
                description="Older description",
                config=ConfigDict(title="Older title", json_schema_extra={"x-old": True}),
            )
        )
    )
    assert schemas["2000-01-01"][ConfiguredModel].model_json_schema() == {
        "title": "Older title",
        "description": "Older description",
        "x-old": True,
        "additionalProperties": True,
        "properties": {"value": {"title": "Value", "type": "string"}},
        "required": ["value"],
        "type": "object",
    }
    assert "x-old" not in schemas["2001-01-01"][ConfiguredModel].model_json_schema()


class AliasedModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    value: str = Field(alias="wire")


@pytest.mark.parametrize("config", [ConfigDict(populate_by_name=False), ConfigDict(validate_by_name=False)])
def test__schema_had_config__name_validation_disabled__should_require_alias(
    create_runtime_schemas: CreateRuntimeSchemas,
    config: ConfigDict,
):
    schemas = create_runtime_schemas(version_change(schema(AliasedModel).had(config=config)))
    older = schemas["2000-01-01"][AliasedModel]
    with pytest.raises(ValidationError, match="missing"):
        older.model_validate({"value": "x"})
    assert older.model_validate({"wire": "x"}).value == "x"
    assert schemas["2001-01-01"][AliasedModel].model_validate({"value": "x"}).value == "x"


def test__schema_had_config__alias_validation_disabled__should_require_name(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(AliasedModel).had(config=ConfigDict(validate_by_alias=False)))
    )
    older = schemas["2000-01-01"][AliasedModel]
    with pytest.raises(ValidationError, match="missing"):
        older.model_validate({"wire": "x"})
    assert older.model_validate({"value": "x"}).value == "x"
    assert schemas["2001-01-01"][AliasedModel].model_validate({"wire": "x"}).value == "x"


def test__schema_had_config__arbitrary_type_in_older_field__should_accept_instances(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class CustomType:
        pass

    schemas = create_runtime_schemas(
        version_change(
            schema(ConfiguredModel).had(config=ConfigDict(arbitrary_types_allowed=True)),
            schema(ConfiguredModel).field("value").had(type=CustomType),
        )
    )
    value = CustomType()
    older = schemas["2000-01-01"][ConfiguredModel]
    assert older.model_validate({"value": value}).value is value
    with pytest.raises(ValidationError, match="is_instance_of"):
        older.model_validate({"value": "x"})
    assert schemas["2001-01-01"][ConfiguredModel](value="x").value == "x"


@pytest.mark.parametrize(
    "config", [ConfigDict(extra="allow"), ConfigDict(frozen=False), ConfigDict(title="Current title")]
)
def test__schema_had_config__unchanged_value__should_raise_error(
    create_runtime_schemas: CreateRuntimeSchemas,
    config: ConfigDict,
):
    key = next(iter(config))
    with pytest.raises(InvalidGenerationInstructionError, match=rf"ConfiguredModel.*{key}.*already"):
        create_runtime_schemas(version_change(schema(ConfiguredModel).had(config=config)))


@pytest.mark.parametrize(
    "key",
    [
        "not_a_config_key",
        "alias_generator",
        "field_title_generator",
        "model_title_generator",
        "ignored_types",
        "use_attribute_docstrings",
        "protected_namespaces",
        "defer_build",
        "plugin_settings",
        "schema_generator",
        "strict",
    ],
)
def test__schema_had_config__unsupported_key__should_raise_clear_error(key: str):
    with pytest.raises(CadwynStructureError, match=rf"ConfiguredModel.*{key}.*(unknown|cannot|unsupported)"):
        schema(ConfiguredModel).had(config={key: True})


def test__schema_had_config__empty_patch__should_raise_error():
    with pytest.raises(CadwynStructureError, match="at least one"):
        schema(ConfiguredModel).had(config=ConfigDict())


class Child(ConfiguredModel):
    pass


class Sibling(ConfiguredModel):
    pass


def test__schema_had_config__leaf_subclass__should_preserve_parent_and_sibling(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(Child).had(config=ConfigDict(extra="forbid"))))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        schemas["2000-01-01"][Child].model_validate({"value": "x", "unknown": 1})
    for model in (ConfiguredModel, Sibling):
        assert schemas["2000-01-01"][model].model_validate({"value": "x", "unknown": 1}).model_dump() == {
            "value": "x",
            "unknown": 1,
        }


@pytest.mark.parametrize("nested", [False, True])
def test__schema_had_config__parent_with_route_subclass__should_fail_startup(
    create_versioned_clients: CreateVersionedClients,
    nested: bool,
):
    class Envelope(BaseModel):
        child: Child

    router = VersionedAPIRouter()

    @router.get("/test", response_model=Envelope if nested else Child)
    def endpoint() -> Any:
        raise AssertionError("Startup must reject the config instruction before serving requests")

    with pytest.raises(InvalidGenerationInstructionError, match=r"ConfiguredModel.*Child.*leaf"):
        create_versioned_clients(
            version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid"))),
            router=router,
        )


def test__schema_had_config__subclass_with_instructions__should_fail_without_routes(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError, match=r"ConfiguredModel.*Child.*leaf"):
        create_runtime_schemas(
            version_change(
                schema(ConfiguredModel).had(config=ConfigDict(extra="forbid")),
                schema(Child).had(name="OldChild"),
            )
        )


def test__schema_had_config__unmigrated_response_extra__should_fail_validation(
    create_versioned_clients: CreateVersionedClients,
):
    from fastapi.exceptions import ResponseValidationError

    router = VersionedAPIRouter()

    @router.get("/test", response_model=ConfiguredModel)
    def endpoint():
        return {"value": "x", "unknown": 1}

    clients = create_versioned_clients(
        version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid"))),
        router=router,
    )
    with pytest.raises(ResponseValidationError, match="extra_forbidden"):
        clients["2000-01-01"].get("/test")
    assert clients["2001-01-01"].get("/test").json() == {"value": "x", "unknown": 1}


def test__schema_had_config__request_extra__should_reject_before_migrations(
    create_versioned_clients: CreateVersionedClients,
):
    from cadwyn import RequestInfo, convert_request_to_next_version_for

    migrated: list[object] = []
    router = VersionedAPIRouter()

    @router.post("/test")
    def endpoint(body: ConfiguredModel) -> ConfiguredModel:
        return body

    @convert_request_to_next_version_for(ConfiguredModel)
    def record_request(request: RequestInfo):
        migrated.append(request.body)
        request.body.pop("unknown", None)

    clients = create_versioned_clients(
        version_change(
            schema(ConfiguredModel).had(config=ConfigDict(extra="forbid")),
            record_request=record_request,
        ),
        router=router,
    )
    response = clients["2000-01-01"].post("/test", json={"value": "x", "unknown": 1})
    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    assert migrated == []
    assert clients["2000-01-01"].post("/test", json={"value": "x"}).json() == {"value": "x"}
    assert migrated == [{"value": "x"}]


def test__schema_had_config__repeated_patch_across_versions__should_reject_noop(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError, match=r"ConfiguredModel.*extra.*already"):
        create_runtime_schemas(
            version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid"))),
            version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid"))),
        )


def test__schema_had_config__callable_json_schema_extra__should_change_older_documentation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    def add_example(json_schema: dict[str, Any]) -> None:
        json_schema["examples"] = [{"value": "legacy"}]

    schemas = create_runtime_schemas(
        version_change(
            schema(ConfiguredModel).had(config=ConfigDict(json_schema_extra=add_example)),
        )
    )
    assert schemas["2000-01-01"][ConfiguredModel].model_json_schema()["examples"] == [{"value": "legacy"}]
    assert "examples" not in schemas["2001-01-01"][ConfiguredModel].model_json_schema()


def test__schema_had_config__populate_by_name_and_explicit_flags__should_use_explicit_flags(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class AliasOnlyModel(BaseModel):
        value: str = Field(alias="wire")

    schemas = create_runtime_schemas(
        version_change(
            schema(AliasOnlyModel).had(
                config=ConfigDict(populate_by_name=True, validate_by_alias=False),
            )
        )
    )
    older = schemas["2000-01-01"][AliasOnlyModel]
    assert older.model_validate({"value": "x"}).value == "x"
    with pytest.raises(ValidationError, match="missing"):
        older.model_validate({"wire": "x"})


def test__schema_had_config__unchanged_populate_by_name__should_reject_noop(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    with pytest.raises(InvalidGenerationInstructionError, match=r"AliasedModel.*populate_by_name.*already"):
        create_runtime_schemas(version_change(schema(AliasedModel).had(config=ConfigDict(populate_by_name=True))))
