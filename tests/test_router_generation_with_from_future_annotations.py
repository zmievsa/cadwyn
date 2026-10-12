from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel, Field, WithJsonSchema

from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle, VersionChange
from tests.conftest import version_change


class OuterSchema(BaseModel):
    bar: MySchema

    extra_annotated: Annotated[str, WithJsonSchema({"type": "string", "description": "Hello"})] = ""


class MySchema(BaseModel):
    foo: str = Field(coerce_numbers_to_str=True)


class MyVersionChange(VersionChange):
    description = "Hello"
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(type=int),
        schema(MySchema).field("foo").didnt_have("coerce_numbers_to_str"),
    )


app = Cadwyn(versions=VersionBundle(Version("2001-01-01", MyVersionChange), Version("2000-01-01")))
router = VersionedAPIRouter()


@router.post("/test")
async def route_with_inner_schema_forwardref(dep: MySchema) -> MySchema:
    return dep


@router.post("/test2")
async def route_with_outer_schema_forwardref(dep: OuterSchema) -> OuterSchema:
    return dep


app.generate_and_include_versioned_routers(router)


def test__router_generation__using_forwardref_inner_global_schema_in_body():
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test", json={"foo": 1}).json() == {"foo": 1}
    assert client_2001.post("/test", json={"foo": 1}).json() == {"foo": "1"}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == 200
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == 200


def test__router_generation__using_forwardref_outer_global_schema_in_body():
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test2", json={"bar": {"foo": 1}}).json() == {"bar": {"foo": 1}, "extra_annotated": ""}
    assert client_2001.post("/test2", json={"bar": {"foo": 1}}).json() == {"bar": {"foo": "1"}, "extra_annotated": ""}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == 200
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == 200


class CallableClassDependency:
    """A callable class to be used as a dependency (with forward ref annotation due to future annotations)."""

    def __init__(self, label: str):
        super().__init__()
        self.label = label

    async def __call__(self, request: Request) -> None:
        pass


def test__router_generation__using_callable_class_dependency_with_forwardref():
    """Test that callable class instances work as dependencies with future annotations.

    Regression test for https://github.com/zmievsa/cadwyn/issues/321
    """

    class EmptyVersionChange(VersionChange):
        description = "Empty version change"
        instructions_to_migrate_to_previous_version = ()

    test_router = VersionedAPIRouter()

    @test_router.get("/run", dependencies=[Depends(CallableClassDependency("route-level"))])
    async def run():
        return {"status": "ok"}

    test_app = Cadwyn(versions=VersionBundle(Version("2001-01-01", EmptyVersionChange), Version("2000-01-01")))
    test_app.generate_and_include_versioned_routers(test_router)

    client = TestClient(test_app, headers={test_app.router.api_version_parameter_name: "2000-01-01"})
    response = client.get("/run")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class TaskOutcome(str, Enum):
    failed = "failed"
    stopped = "stopped"


class TaskOutcomePayload(BaseModel):
    state: TaskOutcome
    hostname: str | None = None
    pid: int | None = None


@pytest.mark.parametrize("version", ["2001-01-01", "2000-01-01"])
def test__router_generation__forwardref_body_matches_version(router, create_versioned_clients, version):
    @router.post("/outcome")
    def report_outcome(payload: TaskOutcomePayload):
        return payload

    client = create_versioned_clients(
        version_change(
            enum(TaskOutcome).didnt_have("stopped"),
            schema(TaskOutcomePayload).field("hostname").didnt_exist,
            schema(TaskOutcomePayload).field("pid").didnt_exist,
        )
    )[version]
    response = client.get(f"/openapi.json?version={version}")

    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    expected_fields = {"state", "hostname", "pid"} if version == "2001-01-01" else {"state"}
    assert set(schemas["TaskOutcomePayload"]["properties"]) == expected_fields
    expected_outcomes = ["failed", "stopped"] if version == "2001-01-01" else ["failed"]
    assert schemas["TaskOutcome"]["enum"] == expected_outcomes

    response = client.post("/outcome", json={"state": "stopped"})

    assert response.status_code == (200 if version == "2001-01-01" else 422)


class ConstrainedFieldsInFieldDefaults(BaseModel):
    min_length_field: str = Field(min_length=1)
    max_length_field: str = Field(max_length=10)
    ge_field: int = Field(ge=0)
    le_field: int = Field(le=100)
    pattern_field: str = Field(pattern="^a")
    alias_field: str = Field(default="", title="Alias Field")
    description_field: str = Field(default="", description="New description")


class ConstrainedFieldsInAnnotated(BaseModel):
    min_length_field: Annotated[str, Field(min_length=1)]
    max_length_field: Annotated[str, Field(max_length=10)]
    ge_field: Annotated[int, Field(ge=0)]
    le_field: Annotated[int, Field(le=100)]
    pattern_field: Annotated[str, Field(pattern="^a")]
    alias_field: Annotated[str, Field(title="Alias Field")] = ""
    description_field: Annotated[str, Field(description="New description")] = ""


PAYLOAD_VALID_IN_ALL_VERSIONS = {
    "min_length_field": "aaaaaa",
    "max_length_field": "ab",
    "ge_field": 10,
    "le_field": 1,
    "pattern_field": "ab",
}


@pytest.fixture
def constrained_fields_router(router: VersionedAPIRouter) -> VersionedAPIRouter:
    @router.post("/field_defaults")
    async def field_defaults(payload: ConstrainedFieldsInFieldDefaults):
        return payload.model_dump()

    @router.post("/annotated")
    async def annotated(payload: ConstrainedFieldsInAnnotated):
        return payload.model_dump()

    return router


@pytest.mark.parametrize(
    ("path", "schema_name"),
    [
        ("/field_defaults", "ConstrainedFieldsInFieldDefaults"),
        ("/annotated", "ConstrainedFieldsInAnnotated"),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "old_attributes", "head_json_schema", "old_json_schema", "value_invalid_only_in_old_version"),
    [
        ("min_length_field", {"min_length": 5}, {"minLength": 1}, {"minLength": 5}, "ab"),
        ("max_length_field", {"max_length": 3}, {"maxLength": 10}, {"maxLength": 3}, "abcdef"),
        ("ge_field", {"ge": 5}, {"minimum": 0}, {"minimum": 5}, 1),
        ("le_field", {"le": 5}, {"maximum": 100}, {"maximum": 5}, 50),
        ("pattern_field", {"pattern": "^ab"}, {"pattern": "^a"}, {"pattern": "^ab"}, "ac"),
    ],
)
def test__router_generation__field_had_constraint_that_was_already_set__should_override_it(
    constrained_fields_router: VersionedAPIRouter,
    create_versioned_clients,
    path: str,
    schema_name: str,
    field_name: str,
    old_attributes: dict[str, Any],
    head_json_schema: dict[str, object],
    old_json_schema: dict[str, object],
    value_invalid_only_in_old_version: object,
):
    clients = create_versioned_clients(
        version_change(
            schema(ConstrainedFieldsInFieldDefaults).field(field_name).had(**old_attributes),
            schema(ConstrainedFieldsInAnnotated).field(field_name).had(**old_attributes),
        ),
        router=constrained_fields_router,
    )
    payload = PAYLOAD_VALID_IN_ALL_VERSIONS | {field_name: value_invalid_only_in_old_version}

    for version, expected_json_schema, expected_status_code in [
        ("2001-01-01", head_json_schema, 200),
        ("2000-01-01", old_json_schema, 422),
    ]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"][schema_name]["properties"][field_name]
        assert field_schema.items() >= expected_json_schema.items()

        assert client.post(path, json=PAYLOAD_VALID_IN_ALL_VERSIONS).status_code == 200
        assert client.post(path, json=payload).status_code == expected_status_code


@pytest.mark.parametrize(
    ("path", "schema_name"),
    [
        ("/field_defaults", "ConstrainedFieldsInFieldDefaults"),
        ("/annotated", "ConstrainedFieldsInAnnotated"),
    ],
)
def test__router_generation__field_had_alias_and_description__should_be_applied(
    constrained_fields_router: VersionedAPIRouter,
    create_versioned_clients,
    path: str,
    schema_name: str,
):
    clients = create_versioned_clients(
        version_change(
            schema(ConstrainedFieldsInFieldDefaults).field("alias_field").had(alias="old_alias"),
            schema(ConstrainedFieldsInFieldDefaults).field("description_field").had(description="Old description"),
            schema(ConstrainedFieldsInAnnotated).field("alias_field").had(alias="old_alias"),
            schema(ConstrainedFieldsInAnnotated).field("description_field").had(description="Old description"),
        ),
        router=constrained_fields_router,
    )

    for version, alias, description in [
        ("2001-01-01", "alias_field", "New description"),
        ("2000-01-01", "old_alias", "Old description"),
    ]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        properties = openapi["components"]["schemas"][schema_name]["properties"]
        assert alias in properties
        assert properties["description_field"]["description"] == description


class FieldWithConstraintAfterValidator(BaseModel):
    foo: Annotated[str, AfterValidator(str.strip), Field(min_length=1)]


def test__router_generation__field_had_constraint_declared_after_validator__should_still_apply_after_it(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: FieldWithConstraintAfterValidator):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(FieldWithConstraintAfterValidator).field("foo").had(min_length=3)),
    )

    assert clients["2001-01-01"].post("/test", json={"foo": "  ab  "}).json() == {"foo": "ab"}
    assert clients["2001-01-01"].post("/test", json={"foo": "   "}).status_code == 422
    assert clients["2000-01-01"].post("/test", json={"foo": "  abc  "}).json() == {"foo": "abc"}
    assert clients["2000-01-01"].post("/test", json={"foo": "  ab  "}).status_code == 422
