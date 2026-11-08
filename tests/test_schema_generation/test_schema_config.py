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


def test__schema_had_config__name_validation_disabled__should_require_alias(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(
        version_change(schema(AliasedModel).had(config=ConfigDict(validate_by_name=False)))
    )
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


@pytest.mark.parametrize("key", ["not_a_config_key", "alias_generator", "populate_by_name", "strict"])
def test__schema_had_config__unsupported_key__should_list_supported_keys(key: str):
    with pytest.raises(
        CadwynStructureError,
        match=rf'"{key}" of "ConfiguredModel".*only these config keys: arbitrary_types_allowed, extra, frozen,',
    ):
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
@pytest.mark.parametrize("in_head", [False, True])
def test__schema_had_config__parent_with_route_subclass__should_fail_startup(
    create_versioned_clients: CreateVersionedClients,
    nested: bool,
    in_head: bool,
):
    class Envelope(BaseModel):
        child: Child

    router = VersionedAPIRouter()

    @router.get("/test", response_model=Envelope if nested else Child)
    def endpoint() -> Any:
        raise AssertionError("Startup must reject the config instruction before serving requests")

    change = version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid")))
    with pytest.raises(InvalidGenerationInstructionError, match=r"ConfiguredModel.*Child.*leaf"):
        create_versioned_clients(
            *(() if in_head else (change,)),
            head_version_changes=(change,) if in_head else (),
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


def test__schema_had_config__alias_validation_enabled__should_keep_name_validation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class NameOnlyModel(BaseModel):
        model_config = ConfigDict(validate_by_alias=False)
        value: str = Field(alias="wire")

    schemas = create_runtime_schemas(
        version_change(schema(NameOnlyModel).had(config=ConfigDict(validate_by_alias=True)))
    )
    older = schemas["2000-01-01"][NameOnlyModel]
    assert older.model_validate({"wire": "x"}).value == "x"
    assert older.model_validate({"value": "x"}).value == "x"


def test__schema_had_config__generic_name_validation_disabled__should_apply_to_parametrization(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    from typing import Generic, TypeVar

    ValueT = TypeVar("ValueT")

    class Bag(BaseModel, Generic[ValueT]):
        # Pydantic ignores validate_by_alias=False here because of populate_by_name
        model_config = ConfigDict(populate_by_name=True, validate_by_alias=False)
        value: ValueT = Field(alias="wire")

    schemas = create_runtime_schemas(version_change(schema(Bag).had(config=ConfigDict(validate_by_name=False))))
    older = schemas["2000-01-01"][Bag[int]]
    with pytest.raises(ValidationError, match="missing"):
        older.model_validate({"value": 1})
    assert older.model_validate({"wire": 1}).value == 1


@pytest.mark.parametrize("parametrized", [False, True])
def test__schema_had_config__generic_route__should_version_config_and_type_arguments(
    create_versioned_clients: CreateVersionedClients,
    parametrized: bool,
):
    from typing import Generic, TypeVar

    class Item(BaseModel):
        value: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Bag(BaseModel, Generic[ItemT]):
        model_config = ConfigDict(extra="allow")
        item: ItemT

    model = Bag[Item] if parametrized else Bag
    router = VersionedAPIRouter()

    def echo(body: BaseModel) -> BaseModel:
        return body

    echo.__annotations__["body"] = model
    router.post("/bags", response_model=model)(echo)
    clients = create_versioned_clients(
        version_change(
            schema(Item).field("value").had(description="Older value"),
            schema(Bag).had(config=ConfigDict(extra="forbid")),
        ),
        router=router,
    )
    older = clients["2000-01-01"]
    newer = clients["2001-01-01"]
    response = older.post("/bags", json={"item": {"value": "x"}})
    assert response.status_code == 200, response.json()
    assert response.json() == {"item": {"value": "x"}}
    response = older.post("/bags", json={"item": {"value": "x"}, "unknown": 1})
    assert response.status_code == 422, response.json()
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    response = newer.post("/bags", json={"item": {"value": "x"}, "unknown": 1})
    assert response.status_code == 200, response.json()
    assert response.json() == {"item": {"value": "x"}, "unknown": 1}
    openapi = older.get("/openapi.json?version=2000-01-01").json()
    ref = openapi["paths"]["/bags"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    bag_schema = openapi["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert bag_schema["additionalProperties"] is False
    item_ref = bag_schema["properties"]["item"]["$ref"]
    item_schema = openapi["components"]["schemas"][item_ref.rsplit("/", 1)[1]]
    assert item_schema["properties"]["value"]["description"] == "Older value"


def test__schema_had_config__real_subclass_of_generic__should_fail_startup(
    create_versioned_clients: CreateVersionedClients,
):
    from typing import Generic, TypeVar

    class Item(BaseModel):
        value: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Bag(BaseModel, Generic[ItemT]):
        item: ItemT

    class SpecialBag(Bag[Item]):
        pass

    router = VersionedAPIRouter()
    router.get("/bags", response_model=SpecialBag)(lambda: {"item": {"value": "x"}})
    with pytest.raises(InvalidGenerationInstructionError, match=r"Bag.*SpecialBag.*leaf"):
        create_versioned_clients(version_change(schema(Bag).had(config=ConfigDict(extra="forbid"))), router=router)


def test__schema_had_config__parametrization_patch__should_override_generic_patch_only_for_that_parametrization(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    from typing import Generic, TypeVar

    ValueT = TypeVar("ValueT")

    class Bag(BaseModel, Generic[ValueT]):
        model_config = ConfigDict(extra="allow")
        item: ValueT
        label: str

    schemas = create_runtime_schemas(
        version_change(
            schema(Bag).had(config=ConfigDict(extra="forbid")),
            schema(Bag[int]).had(config=ConfigDict(extra="ignore", str_strip_whitespace=True)),
        )
    )
    older = schemas["2000-01-01"]
    assert older[Bag[int]].model_validate({"item": 1, "label": " x ", "unknown": 2}).model_dump() == {
        "item": 1,
        "label": "x",
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        older[Bag[str]].model_validate({"item": "value", "label": " x ", "unknown": 2})
    assert schemas["2001-01-01"][Bag[int]].model_validate({"item": 1, "label": " x ", "unknown": 2}).model_dump() == {
        "item": 1,
        "label": " x ",
        "unknown": 2,
    }


def test__schema_had_config__parametrization_restores_newer_generic_value__should_compare_current_version(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    from typing import Generic, TypeVar

    ValueT = TypeVar("ValueT")

    class Bag(BaseModel, Generic[ValueT]):
        model_config = ConfigDict(extra="allow")
        item: ValueT

    schemas = create_runtime_schemas(
        version_change(schema(Bag[int]).had(config=ConfigDict(extra="allow"))),
        version_change(schema(Bag).had(config=ConfigDict(extra="forbid"))),
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        schemas["2001-01-01"][Bag[int]].model_validate({"item": 1, "unknown": 2})
    assert schemas["2000-01-01"][Bag[int]].model_validate({"item": 1, "unknown": 2}).model_dump() == {
        "item": 1,
        "unknown": 2,
    }


def test__schema_had_config__head_leaf__should_apply_to_every_served_version(
    create_versioned_clients: CreateVersionedClients,
):
    router = VersionedAPIRouter()

    @router.post("/test")
    def echo(body: ConfiguredModel) -> ConfiguredModel:
        return body

    clients = create_versioned_clients(
        version_change(),
        head_version_changes=(version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="forbid"))),),
        router=router,
    )
    for client in clients.values():
        response = client.post("/test", json={"value": "x", "unknown": 1})
        assert response.status_code == 422, response.json()
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        response = client.post("/test", json={"value": "x"})
        assert response.status_code == 200, response.json()
        assert response.json() == {"value": "x"}


def test__schema_had_config__head_noop__should_reject_at_startup(create_versioned_clients: CreateVersionedClients):
    with pytest.raises(InvalidGenerationInstructionError, match=r"ConfiguredModel.*extra.*already"):
        create_versioned_clients(
            head_version_changes=(version_change(schema(ConfiguredModel).had(config=ConfigDict(extra="allow"))),),
        )
