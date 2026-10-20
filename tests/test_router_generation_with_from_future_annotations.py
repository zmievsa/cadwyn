from __future__ import annotations

import dataclasses
import functools
import inspect
import sys
import types
import typing
import warnings
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Optional

import pytest
from annotated_types import Len, MinLen
from fastapi import Body, Depends, Query, Request
from fastapi.testclient import TestClient
from pydantic import (
    AfterValidator,
    AliasChoices,
    AllowInfNan,
    BaseModel,
    ConfigDict,
    Field,
    PydanticUserError,
    Strict,
    StringConstraints,
    WithJsonSchema,
    conint,
)
from pydantic.fields import FieldInfo
from typing_extensions import Self, override

from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.data import RequestInfo, convert_request_to_next_version_for
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle, VersionChange
from tests._data import postponed_annotations_dependencies as other_module
from tests.conftest import version_change

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal


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
    ("path", "schema_name", "is_applied"),
    [
        # Cadwyn 7.4.0 ignored had() for a constraint that the annotation's metadata sets, including the annotation
        # that pydantic rebuilds from Field() with postponed annotations. Applying it would make the older versions
        # reject the requests and responses that they have always accepted, so it stays ignored
        ("/field_defaults", "ConstrainedFieldsInFieldDefaults", False),
        ("/annotated", "ConstrainedFieldsInAnnotated", False),
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
def test__router_generation__field_had_constraint_that_was_already_set__should_override_it_unless_annotation_sets_it(
    constrained_fields_router: VersionedAPIRouter,
    create_versioned_clients,
    path: str,
    schema_name: str,
    is_applied: bool,
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
        ("2000-01-01", old_json_schema, 422) if is_applied else ("2000-01-01", head_json_schema, 200),
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


class ItemWithAliasInAnnotated(BaseModel):
    name: Annotated[str, Field(alias="a")]


# With postponed annotations, Cadwyn rebuilds the annotation without the Field() so didnt_have() deletes the alias
@pytest.mark.parametrize(
    ("attributes", "expected_warnings", "old_key"),
    [
        pytest.param(
            ("alias",),
            [
                (
                    'schema(ItemWithAliasInAnnotated).field("name").didnt_have("alias") in "MyVersionChange" '
                    'changes the alias of "ItemWithAliasInAnnotated.name", but in API version "2000-01-01" '
                    'its validation_alias and serialization_alias are still "a", so that version keeps using '
                    '"a" instead of the key that the field has without an alias. Cadwyn 9.0 will change them '
                    "too. To use the key that the field has without an alias now and silence this warning, "
                    'also pass "validation_alias" and "serialization_alias" to this .didnt_have(). To keep '
                    '"a", remove "alias" from it instead.'
                )
            ],
            "a",
            id="alias",
        ),
        pytest.param(("alias", "validation_alias", "serialization_alias"), [], "name", id="alias_and_its_keys"),
    ],
)
def test__router_generation__annotated_field_didnt_have_alias__should_warn_unless_its_keys_are_deleted_too(
    router: VersionedAPIRouter,
    create_versioned_clients,
    attributes: tuple[Any, ...],
    expected_warnings: list[str],
    old_key: str,
):
    @router.post("/items", response_model=ItemWithAliasInAnnotated)
    async def echo(item: ItemWithAliasInAnnotated):
        raise NotImplementedError

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        clients = create_versioned_clients(
            version_change(schema(ItemWithAliasInAnnotated).field("name").didnt_have(*attributes))
        )

    assert [str(warning.message) for warning in caught if warning.category is FutureWarning] == expected_warnings
    openapi = clients["2000-01-01"].get("/openapi.json?version=2000-01-01").json()
    assert list(openapi["components"]["schemas"]["ItemWithAliasInAnnotated"]["properties"]) == [old_key]


class ParentWithInheritedField(BaseModel):
    foo: str = Field(default="", description="Head description", max_length=10)
    bar: int = 0


class ChildWithInheritedField(ParentWithInheritedField):
    pass


class SiblingWithInheritedField(ParentWithInheritedField):
    pass


def test__router_generation__child_had_inherited_field__should_not_change_parent_and_siblings(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/parent", response_model=ParentWithInheritedField)
    async def parent(payload: ParentWithInheritedField):
        return payload

    @router.post("/child", response_model=ChildWithInheritedField)
    async def child(payload: ChildWithInheritedField):
        return payload

    @router.post("/sibling", response_model=SiblingWithInheritedField)
    async def sibling(payload: SiblingWithInheritedField):
        return payload

    clients = create_versioned_clients(
        version_change(schema(ParentWithInheritedField).field("foo").had(description="Older description")),
        version_change(
            schema(ParentWithInheritedField).field("bar").had(description="Bar"),
            schema(ChildWithInheritedField).field("foo").had(description="Child description", max_length=3),
        ),
        router=router,
    )

    # The changes of the parent's field in even older versions still apply to the child.
    # With postponed annotations, max_length=10 is also in the annotation that pydantic rebuilds from Field() and
    # Cadwyn 7.4.0 ignored had(max_length=3) for it. Applying it would make the older versions reject the requests
    # and responses that they have always accepted, so it stays ignored (test_router_generation.py checks that it
    # does not leak into the parent and siblings without postponed annotations)
    for version, path, schema_name, description, max_length in [
        ("2001-01-01", "/parent", "ParentWithInheritedField", "Head description", 10),
        ("2001-01-01", "/child", "ChildWithInheritedField", "Child description", 10),
        ("2001-01-01", "/sibling", "SiblingWithInheritedField", "Head description", 10),
        ("2000-01-01", "/parent", "ParentWithInheritedField", "Older description", 10),
        ("2000-01-01", "/child", "ChildWithInheritedField", "Older description", 10),
        ("2000-01-01", "/sibling", "SiblingWithInheritedField", "Older description", 10),
    ]:
        client = clients[version]
        assert client.post(path, json={"foo": "a" * 3}).status_code == 200
        assert client.post(path, json={"foo": "a" * 4}).status_code == (200 if max_length == 10 else 422)
        openapi = client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"][schema_name]["properties"]["foo"]
        assert field_schema["description"] == description
        assert field_schema["maxLength"] == max_length


class Invoice(BaseModel):
    code: str
    amount: int


class BusinessInvoice(Invoice):
    pass


class PersonalInvoice(Invoice):
    pass


def test__router_generation__child_had_constraint_of_inherited_field__should_not_change_request_bodies_of_sibling(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/business")
    async def create_business(payload: BusinessInvoice) -> BusinessInvoice:
        return payload

    @router.post("/personal")
    async def create_personal(payload: PersonalInvoice) -> PersonalInvoice:
        return payload

    clients = create_versioned_clients(
        version_change(
            schema(Invoice).field("amount").had(description="Amount"),
            schema(BusinessInvoice).field("code").had(max_length=5),
        ),
        router=router,
    )

    # The parent has an instruction of its own (for "amount"). With postponed annotations, request bodies are
    # validated with the versioned models, so the leak of #466 would reject the personal invoice too
    body = {"code": "LONG-CODE", "amount": 1}
    assert clients["2001-01-01"].post("/business", json=body).status_code == 200
    assert clients["2001-01-01"].post("/personal", json=body).status_code == 200
    assert clients["2000-01-01"].post("/business", json=body).status_code == 422
    assert clients["2000-01-01"].post("/personal", json=body).json() == body


class DeletableAttributesInFieldDefaults(BaseModel):
    min_length_field: str = Field(min_length=3)
    max_length_field: str = Field(max_length=3)
    ge_field: int = Field(ge=5)
    le_field: int = Field(le=5)
    pattern_field: str = Field(pattern="^a")
    description_field: str = Field(default="", description="Custom description")
    title_field: str = Field(default="", title="Custom title")
    validated_title_field: Annotated[str, AfterValidator(str.strip)] = Field(default="", title="Custom title")


class DeletableAttributesInAnnotated(BaseModel):
    min_length_field: Annotated[str, Field(min_length=3)]
    max_length_field: Annotated[str, Field(max_length=3)]
    ge_field: Annotated[int, Field(ge=5)]
    le_field: Annotated[int, Field(le=5)]
    pattern_field: Annotated[str, Field(pattern="^a")]
    description_field: Annotated[str, Field(description="Custom description")] = ""
    title_field: Annotated[str, Field(title="Custom title")] = ""
    validated_title_field: Annotated[str, AfterValidator(str.strip), Field(title="Custom title")] = ""


PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS = {
    "min_length_field": "abc",
    "max_length_field": "abc",
    "ge_field": 5,
    "le_field": 5,
    "pattern_field": "abc",
}


@pytest.fixture
def deletable_attributes_router(router: VersionedAPIRouter) -> VersionedAPIRouter:
    @router.post("/field_defaults")
    async def field_defaults(payload: DeletableAttributesInFieldDefaults):
        return payload.model_dump()

    @router.post("/annotated")
    async def annotated(payload: DeletableAttributesInAnnotated):
        return payload.model_dump()

    return router


@pytest.mark.parametrize(
    ("path", "schema_name"),
    [
        ("/field_defaults", "DeletableAttributesInFieldDefaults"),
        ("/annotated", "DeletableAttributesInAnnotated"),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "attribute", "json_schema_key", "value_valid_only_in_old_version"),
    [
        ("min_length_field", "min_length", "minLength", "ab"),
        ("max_length_field", "max_length", "maxLength", "abcdef"),
        ("ge_field", "ge", "minimum", 1),
        ("le_field", "le", "maximum", 50),
        ("pattern_field", "pattern", "pattern", "bc"),
    ],
)
def test__router_generation__field_didnt_have_constraint__should_remove_it(
    deletable_attributes_router: VersionedAPIRouter,
    create_versioned_clients,
    path: str,
    schema_name: str,
    field_name: str,
    attribute: Any,
    json_schema_key: str,
    value_valid_only_in_old_version: object,
):
    @convert_request_to_next_version_for(DeletableAttributesInFieldDefaults, DeletableAttributesInAnnotated)
    def make_request_valid_in_head_version(request: RequestInfo):
        request.body.update(PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS)

    clients = create_versioned_clients(
        version_change(
            schema(DeletableAttributesInFieldDefaults).field(field_name).didnt_have(attribute),
            schema(DeletableAttributesInAnnotated).field(field_name).didnt_have(attribute),
            make_request_valid_in_head_version=make_request_valid_in_head_version,
        ),
        router=deletable_attributes_router,
    )
    payload = PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS | {field_name: value_valid_only_in_old_version}

    for version, expected_status_code in [("2001-01-01", 422), ("2000-01-01", 200)]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"][schema_name]["properties"][field_name]
        assert (json_schema_key in field_schema) == (version == "2001-01-01")

        assert client.post(path, json=PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS).status_code == 200
        assert client.post(path, json=payload).status_code == expected_status_code


@pytest.mark.parametrize(
    ("path", "schema_name"),
    [
        ("/field_defaults", "DeletableAttributesInFieldDefaults"),
        ("/annotated", "DeletableAttributesInAnnotated"),
    ],
)
def test__router_generation__field_didnt_have_description_and_title__should_remove_them(
    deletable_attributes_router: VersionedAPIRouter,
    create_versioned_clients,
    path: str,
    schema_name: str,
):
    clients = create_versioned_clients(
        version_change(
            schema(DeletableAttributesInFieldDefaults).field("description_field").didnt_have("description"),
            schema(DeletableAttributesInFieldDefaults).field("title_field").didnt_have("title"),
            schema(DeletableAttributesInFieldDefaults).field("validated_title_field").didnt_have("title"),
            schema(DeletableAttributesInAnnotated).field("description_field").didnt_have("description"),
            schema(DeletableAttributesInAnnotated).field("title_field").didnt_have("title"),
            schema(DeletableAttributesInAnnotated).field("validated_title_field").didnt_have("title"),
        ),
        router=deletable_attributes_router,
    )

    for version, description, title, validated_title in [
        ("2001-01-01", "Custom description", "Custom title", "Custom title"),
        ("2000-01-01", None, "Title Field", "Validated Title Field"),
    ]:
        client = clients[version]
        openapi = client.get(f"/openapi.json?version={version}").json()
        properties = openapi["components"]["schemas"][schema_name]["properties"]
        assert properties["description_field"].get("description") == description
        assert properties["title_field"]["title"] == title
        assert properties["validated_title_field"]["title"] == validated_title
        assert client.post(path, json=PAYLOAD_WITH_DELETABLE_ATTRIBUTES_VALID_IN_ALL_VERSIONS).status_code == 200


class SchemaWithUnsetAttributesInAnnotated(BaseModel):
    conint_field: conint(lt=7)  # ty: ignore[invalid-type-form]


def test__router_generation__field_didnt_have_attribute_that_annotation_metadata_leaves_unset__should_change_nothing(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: SchemaWithUnsetAttributesInAnnotated):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithUnsetAttributesInAnnotated).field("conint_field").didnt_have("gt")),
    )

    openapi_schemas = [
        client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        for version, client in clients.items()
    ]
    assert openapi_schemas[0] == openapi_schemas[1]
    for client in clients.values():
        assert client.post("/test", json={"conint_field": 6}).status_code == 200
        assert client.post("/test", json={"conint_field": 7}).status_code == 422


class ParentWithClassVar(BaseModel):
    value: ClassVar[str] = "inherited"


with pytest.warns(UserWarning, match='Field name "value" .* shadows an attribute in parent'):

    class ChildRedeclaringClassVarAsField(ParentWithClassVar):
        value: Annotated[str, Field(min_length=2)] = Field()  # ty: ignore[invalid-attribute-override]


@pytest.mark.filterwarnings('ignore:Field name "value" .* shadows an attribute in parent:UserWarning')
def test__router_generation__field_redeclaring_parent_class_var__should_stay_required(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: ChildRedeclaringClassVarAsField):
        return payload.model_dump()

    clients = create_versioned_clients(version_change())

    for version, versioned_client in clients.items():
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["ChildRedeclaringClassVarAsField"]["required"] == ["value"]
        assert versioned_client.post("/test", json={}).status_code == 422
        assert versioned_client.post("/test", json={"value": "ab"}).json() == {"value": "ab"}


class Trimmed(StringConstraints):
    def __init__(self, min_length: int = 2) -> None:
        super().__init__(min_length=min_length, strip_whitespace=True)


class SchemaWithConstraintsSubclass(BaseModel):
    foo: Annotated[str, Trimmed()]


def test__router_generation__field_didnt_have_constraint_of_constraints_subclass_with_custom_init__should_remove_it(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: SchemaWithConstraintsSubclass):
        return payload.model_dump()

    @convert_request_to_next_version_for(SchemaWithConstraintsSubclass)
    def pad_foo(request: RequestInfo):
        request.body["foo"] = request.body["foo"].ljust(2, "_")

    clients = create_versioned_clients(
        version_change(schema(SchemaWithConstraintsSubclass).field("foo").didnt_have("min_length"), pad_foo=pad_foo),
    )

    assert clients["2001-01-01"].post("/test", json={"foo": " a "}).status_code == 422
    assert clients["2001-01-01"].post("/test", json={"foo": " ab "}).json() == {"foo": "ab"}
    assert clients["2000-01-01"].post("/test", json={"foo": " a "}).json() == {"foo": "a_"}


FIELD_INFO_WITH_LEN = FieldInfo.from_annotation(Annotated[str, Len(2, 5)])  # ty: ignore[invalid-argument-type]


class SchemaWithLenConstraints(BaseModel):
    annotated_len: Annotated[str, Len(2, 5)]
    field_info_with_len: Annotated[str, FIELD_INFO_WITH_LEN]


@pytest.mark.parametrize("field_name", ["annotated_len", "field_info_with_len"])
def test__router_generation__field_didnt_have_min_length_of_len__should_keep_max_length(
    router: VersionedAPIRouter,
    create_versioned_clients,
    field_name: str,
):
    @router.post("/test")
    async def route(payload: SchemaWithLenConstraints):
        return payload.model_dump()

    @convert_request_to_next_version_for(SchemaWithLenConstraints)
    def pad_values(request: RequestInfo):
        request.body[field_name] = request.body[field_name].ljust(2, "_")

    clients = create_versioned_clients(
        version_change(
            schema(SchemaWithLenConstraints).field(field_name).didnt_have("min_length"), pad_values=pad_values
        ),
    )
    payload = {"annotated_len": "abc", "field_info_with_len": "abc"}

    for version, min_length, status_code_of_too_short_value in [("2001-01-01", 2, 422), ("2000-01-01", None, 200)]:
        versioned_client = clients[version]
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        field_schema = openapi["components"]["schemas"]["SchemaWithLenConstraints"]["properties"][field_name]
        assert field_schema.get("minLength") == min_length
        assert field_schema["maxLength"] == 5
        assert versioned_client.post("/test", json=payload | {field_name: "a"}).status_code == (
            status_code_of_too_short_value
        )
        assert versioned_client.post("/test", json=payload | {field_name: "abcdef"}).status_code == 422


@dataclasses.dataclass(frozen=True)
class NameConstraints(StringConstraints):
    min_length: int = 2


@dataclasses.dataclass(frozen=True)
class LenWithMinimum(Len):
    min_length: int = 2


class SchemaWithGroupedConstraintSubclasses(BaseModel):
    string_constraints_subclass: Annotated[str, NameConstraints()]
    len_subclass: Annotated[str, LenWithMinimum(max_length=5)]


@pytest.mark.parametrize("field_name", ["string_constraints_subclass", "len_subclass"])
def test__router_generation__field_didnt_have_constraint_with_default_in_grouped_constraint_subclass__should_remove_it(
    router: VersionedAPIRouter,
    create_versioned_clients,
    field_name: str,
):
    @router.post("/test")
    async def route(payload: SchemaWithGroupedConstraintSubclasses):
        return payload.model_dump()

    @convert_request_to_next_version_for(SchemaWithGroupedConstraintSubclasses)
    def pad_values(request: RequestInfo):
        request.body[field_name] = request.body[field_name].ljust(2, "_")

    clients = create_versioned_clients(
        version_change(
            schema(SchemaWithGroupedConstraintSubclasses).field(field_name).didnt_have("min_length"),
            pad_values=pad_values,
        ),
    )
    payload = {"string_constraints_subclass": "abc", "len_subclass": "abc"}

    for version, min_length, status_code_of_empty_value in [("2001-01-01", 2, 422), ("2000-01-01", None, 200)]:
        versioned_client = clients[version]
        openapi = versioned_client.get(f"/openapi.json?version={version}").json()
        properties = openapi["components"]["schemas"]["SchemaWithGroupedConstraintSubclasses"]["properties"]
        assert properties[field_name].get("minLength") == min_length
        assert versioned_client.post("/test", json=payload | {field_name: ""}).status_code == (
            status_code_of_empty_value
        )


class SchemaWithSameConstraintBeforeAndAfterValidator(BaseModel):
    value: Annotated[str, AfterValidator(lambda v: v * 3), MinLen(3)] = Field(min_length=3)


def test__router_generation__same_constraint_before_and_after_validator__should_be_checked_on_both_sides(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: SchemaWithSameConstraintBeforeAndAfterValidator):
        return payload.model_dump()

    clients = create_versioned_clients(version_change())

    for versioned_client in clients.values():
        assert versioned_client.post("/test", json={"value": "a"}).status_code == 422
        assert versioned_client.post("/test", json={"value": "abc"}).status_code == 200


class SchemaWithConstraintClasses(BaseModel):
    strict_field: Annotated[int, Strict]
    allow_inf_nan_field: Annotated[float, AllowInfNan]


@pytest.mark.parametrize(
    ("field_name", "instruction", "value", "head_status_code", "old_status_code"),
    [
        # Cadwyn 7.4.0 ignored had() for constraint classes, like for other constraints in the annotation's metadata.
        # Applying it could make the older versions reject the requests that they have always accepted (as in
        # had(allow_inf_nan=False)), so it stays ignored. didnt_have() failed with TypeError there instead
        ("strict_field", lambda field: field.had(strict=False), "1", 422, 422),
        ("strict_field", lambda field: field.didnt_have("strict"), "1", 422, 200),
        ("allow_inf_nan_field", lambda field: field.had(allow_inf_nan=False), "inf", 200, 200),
        ("allow_inf_nan_field", lambda field: field.didnt_have("allow_inf_nan"), "inf", 200, 200),
    ],
)
def test__router_generation__field_had_and_didnt_have__constraint_class_used_as_metadata(
    router: VersionedAPIRouter,
    create_versioned_clients,
    field_name: str,
    instruction: Callable[[Any], Any],
    value: object,
    head_status_code: int,
    old_status_code: int,
):
    @router.post("/test")
    async def route(payload: SchemaWithConstraintClasses):
        return {"ok": True}

    clients = create_versioned_clients(
        version_change(instruction(schema(SchemaWithConstraintClasses).field(field_name)))
    )
    payload = {"strict_field": 1, "allow_inf_nan_field": 1.0, field_name: value}

    assert clients["2001-01-01"].post("/test", json=payload).status_code == head_status_code
    assert clients["2000-01-01"].post("/test", json=payload).status_code == old_status_code


class SchemaWithDocStringInAnnotated(BaseModel):
    foo: Annotated[str, "Some documentation", Field(title="Custom title")]


def test__router_generation__field_didnt_have_title__with_string_in_annotated__should_remove_it(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: SchemaWithDocStringInAnnotated):
        return payload.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithDocStringInAnnotated).field("foo").didnt_have("title")),
    )

    for version, title in [("2001-01-01", "Custom title"), ("2000-01-01", "Foo")]:
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        assert openapi["components"]["schemas"]["SchemaWithDocStringInAnnotated"]["properties"]["foo"]["title"] == title
        assert clients[version].post("/test", json={"foo": "bar"}).json() == {"foo": "bar"}


class OuterSchemaWithValidationAliases(BaseModel):
    inner: InnerSchemaWithValidationAliases = Field(alias="i", validation_alias="j")
    inners: Annotated[list[InnerSchemaWithValidationAliases], Field(alias="l", validation_alias="m")]


class InnerSchemaWithValidationAliases(BaseModel):
    value: int = Field(alias="x", validation_alias="v")
    other: Annotated[int, Field(validation_alias=AliasChoices("o", "p"), serialization_alias="s")]


def test__router_generation__body_fields_with_validation_aliases__should_reach_head_in_every_version(
    router: VersionedAPIRouter,
    create_versioned_clients,
):
    @router.post("/test")
    async def route(payload: OuterSchemaWithValidationAliases):
        return payload.model_dump()

    @convert_request_to_next_version_for(OuterSchemaWithValidationAliases)
    def migrator(request: RequestInfo):
        request.body["l"].append({"x": 5, "s": 6})

    clients = create_versioned_clients(version_change(migrator=migrator))
    payload = {"j": {"v": 1, "o": 2}, "m": [{"v": 3, "p": 4}]}

    assert clients["2001-01-01"].post("/test", json=payload).json() == {
        "inner": {"value": 1, "other": 2},
        "inners": [{"value": 3, "other": 4}],
    }
    assert clients["2000-01-01"].post("/test", json=payload).json() == {
        "inner": {"value": 1, "other": 2},
        "inners": [{"value": 3, "other": 4}, {"value": 5, "other": 6}],
    }


@functools.lru_cache
def cached_limit(limit: Annotated[int, Query(ge=1)] = 3) -> int:
    return limit


class DecoratorThatKeepsWrapped:
    def __init__(self, func: Any) -> None:
        functools.update_wrapper(self, func)
        self.func = func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


@DecoratorThatKeepsWrapped
def decorated_limit(request: Request, limit: Annotated[int, Query(ge=1)] = 3) -> int:
    return limit


def test__router_generation__decorated_dependencies_with_forwardrefs__should_resolve_annotations(
    router, create_versioned_clients
):
    @router.get("/cached/annotated")
    async def cached_annotated(limit: Annotated[int, Depends(cached_limit)]):
        return limit

    @router.get("/cached/default")
    async def cached_default(limit: int = Depends(cached_limit)):
        return limit

    @router.get("/decorated/annotated")
    async def decorated_annotated(limit: Annotated[int, Depends(decorated_limit)]):
        return limit

    @router.get("/decorated/default")
    async def decorated_default(limit: int = Depends(decorated_limit)):
        return limit

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/cached/annotated", "/cached/default", "/decorated/annotated", "/decorated/default"):
            assert version_client.get(path, params={"limit": 5}).json() == 5
            assert version_client.get(path, params={"limit": 0}).status_code == 422


class ResourceWithForwardRefsInInit:
    def __init__(self, body: MySchema) -> None:
        self.body = body


class ResourceWithForwardRefsInNew:
    body: MySchema

    def __new__(cls, body: MySchema) -> Self:
        resource = super().__new__(cls)
        resource.body = body
        return resource


class ResourceMeta(type):
    @override
    def __call__(cls, body: MySchema) -> Any:
        return super().__call__(body)


class ResourceWithForwardRefsInMetaclass(metaclass=ResourceMeta):
    def __init__(self, body: Any) -> None:
        self.body = body


class ResourceWithoutConstructor:
    body = MySchema(foo="default")


class HashableBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    foo: str


class ResourceWithCachedNew:
    body: HashableBody

    @functools.lru_cache
    def __new__(cls, body: HashableBody) -> Self:
        resource = super().__new__(cls)
        resource.body = body
        return resource


class ResourceWithCachedInit:
    @functools.lru_cache  # noqa: B019
    def __init__(self, body: HashableBody) -> None:
        self.body = body


class StaticCallMeta(type):
    @override
    @staticmethod
    def __call__(body: HashableBody) -> Any:
        return types.SimpleNamespace(body=body)


class ResourceWithStaticMetaclassCall(metaclass=StaticCallMeta):
    pass


class ClassCallMeta(type):
    @override
    @classmethod
    def __call__(cls, body: HashableBody) -> Any:
        return types.SimpleNamespace(body=body)


class ResourceWithClassMetaclassCall(metaclass=ClassCallMeta):
    pass


def test__router_generation__partial_of_class_with_forwardrefs__should_resolve_annotations(
    router, create_versioned_clients
):
    @router.post("/init")
    async def init_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithForwardRefsInInit))]):
        return resource.body

    @router.post("/new")
    async def new_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithForwardRefsInNew))]):
        return resource.body

    @router.post("/metaclass")
    async def metaclass_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithForwardRefsInMetaclass))],
    ):
        return resource.body

    @router.post("/cached_new")
    async def cached_new_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithCachedNew))]):
        return resource.body

    @router.post("/cached_init")
    async def cached_init_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithCachedInit))]):
        return resource.body

    @router.post("/static_metaclass")
    async def static_metaclass_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithStaticMetaclassCall))],
    ):
        return resource.body

    @router.post("/class_metaclass")
    async def class_metaclass_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithClassMetaclassCall))],
    ):
        return resource.body

    @router.get("/no_constructor")
    async def no_constructor_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithoutConstructor))]):
        return resource.body

    paths = ["/init", "/new", "/metaclass", "/cached_new", "/cached_init"]
    # Python 3.10's inspect.signature() drops the first parameter of a staticmethod or classmethod __call__ of
    # a metaclass so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths += ["/static_metaclass", "/class_metaclass"]
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}
        assert version_client.get("/no_constructor").json() == {"foo": "default"}


def _decorator_from_this_module(func: Any) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper


class ResourceWithPartialmethodInit:
    __init__ = functools.partialmethod(other_module.initialize_with_label, label="fixed")


class ResourceWithStaticmethodNew:
    __new__ = staticmethod(other_module.create)


class ClassmethodCallMeta(type):
    __call__ = classmethod(other_module.metaclass_call)


class ResourceWithClassmethodMetaclassCall(metaclass=ClassmethodCallMeta):
    pass


class ResourceWithDecoratedInit:
    __init__ = _decorator_from_this_module(other_module.initialize)


class InheritedPartialmethodInit(ResourceWithPartialmethodInit):
    pass


class InheritedStaticmethodNew(ResourceWithStaticmethodNew):
    pass


# Each of the following wrappers leads to a function from this module through one attribute and to a function from
# the other module through another one. Only the order of inspect.signature() leads to the annotations it returns.


def _invoke(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


# A partial's explicit __wrapped__ comes before its .func
partial_with_wrapped = functools.update_wrapper(
    functools.partial(_invoke, other_module.get_body), other_module.get_body
)


class ResourceWithPartialmethodOfDecoratedInit:
    __init__ = functools.partialmethod(_decorator_from_this_module(other_module.initialize_with_label), label="fixed")


class ResourceWithStaticmethodOfDecoratedNew:
    __new__ = staticmethod(_decorator_from_this_module(other_module.create))


class ClassmethodOfDecoratedCallMeta(type):
    __call__ = classmethod(_decorator_from_this_module(other_module.metaclass_call))


class ResourceWithClassmethodOfDecoratedMetaclassCall(metaclass=ClassmethodOfDecoratedCallMeta):
    pass


class ProviderWithDecoratedMethod:
    provide = _decorator_from_this_module(other_module.provide)


# The decorator's __wrapped__ leads to a bound method whose function comes from the other module
decorated_bound_method = _decorator_from_this_module(other_module.provider.provide)


def test__router_generation__dependencies_with_annotations_from_another_module__should_resolve_them(
    router, create_versioned_clients
):
    @router.post("/partialmethod")
    async def partialmethod_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithPartialmethodInit))]):
        return resource.body

    @router.post("/staticmethod")
    async def staticmethod_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithStaticmethodNew))]):
        return resource.body

    @router.post("/classmethod")
    async def classmethod_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithClassmethodMetaclassCall))],
    ):
        return resource.body

    @router.post("/wrapped")
    async def wrapped_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithDecoratedInit))]):
        return resource.body

    @router.post("/partial")
    async def partial_route(
        body: Annotated[Any, Depends(functools.partial(other_module.get_body_with_label, label="fixed"))],
    ):
        return body

    @router.post("/bound_method")
    async def bound_method_route(body: Annotated[Any, Depends(other_module.provider.provide)]):
        return body

    @router.post("/inherited_partialmethod")
    async def inherited_partialmethod_route(
        resource: Annotated[Any, Depends(functools.partial(InheritedPartialmethodInit))],
    ):
        return resource.body

    @router.post("/inherited_staticmethod")
    async def inherited_staticmethod_route(
        resource: Annotated[Any, Depends(functools.partial(InheritedStaticmethodNew))],
    ):
        return resource.body

    paths = [
        "/partialmethod",
        "/staticmethod",
        "/wrapped",
        "/partial",
        "/bound_method",
        "/inherited_partialmethod",
        "/inherited_staticmethod",
    ]
    # Python 3.10's inspect.signature() drops the first parameter of a classmethod __call__ of a metaclass
    # so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths.append("/classmethod")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


class _PartialWithOverriddenCall(functools.partial):
    @override
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return super().__call__(*args, **kwargs)


# Postponed annotations of routes can only refer to the dependency through the globals
partial_subclass_of_dependency_from_another_module = _PartialWithOverriddenCall(
    other_module.get_body_with_optional_quoted_body
)


def test__router_generation__partial_subclass_of_dependency_from_another_module__should_accept_bodies_like_head(
    router, create_versioned_clients
):
    # FastAPI cannot resolve "Payload" for such partials in the head route (it uses the unwrapped partial's globals,
    # which are empty) and Cadwyn resolves them in the globals of their __call__, where "Payload" does not exist
    # either. Resolving it anywhere else would make the versioned routes expect a different request body than
    # the head route that Cadwyn validates requests against after migrating them
    @router.post("/test")
    async def route(body: Annotated[Any, Depends(partial_subclass_of_dependency_from_another_module)]):
        return body

    for version_client in create_versioned_clients(version_change()).values():
        response = version_client.post("/test", json={"foo": "bar"})
        assert response.status_code == 200, response.json()
        assert response.json() == {"foo": "bar"}


def test__router_generation__wrapped_dependencies_from_another_module__should_resolve_annotations_like_signature(
    router, create_versioned_clients
):
    @router.post("/partial_with_wrapped")
    async def partial_with_wrapped_route(body: Annotated[Any, Depends(partial_with_wrapped)]):
        return body

    @router.post("/partialmethod_of_decorated")
    async def partialmethod_of_decorated_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithPartialmethodOfDecoratedInit))],
    ):
        return resource.body

    @router.post("/staticmethod_of_decorated")
    async def staticmethod_of_decorated_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithStaticmethodOfDecoratedNew))],
    ):
        return resource.body

    @router.post("/classmethod_of_decorated")
    async def classmethod_of_decorated_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithClassmethodOfDecoratedMetaclassCall))],
    ):
        return resource.body

    @router.post("/bound_method_of_decorated")
    async def bound_method_of_decorated_route(body: Annotated[Any, Depends(ProviderWithDecoratedMethod().provide)]):
        return body

    @router.post("/decorated_bound_method")
    async def decorated_bound_method_route(body: Annotated[Any, Depends(decorated_bound_method)]):
        return body

    paths = [
        "/partial_with_wrapped",
        "/partialmethod_of_decorated",
        "/staticmethod_of_decorated",
        "/bound_method_of_decorated",
        "/decorated_bound_method",
    ]
    # Python 3.10's inspect.signature() drops the first parameter of a classmethod __call__ of a metaclass
    # so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths.append("/classmethod_of_decorated")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


class ProviderWithPropertyCall:
    def __init__(self, function: Any) -> None:
        self.function = function

    @property
    def __call__(self) -> Any:
        return self.function


class _InstanceFunctionDescriptor:
    def __get__(self, instance: Any, owner: type) -> Any:
        return instance.function


class ProviderWithDescriptorCall:
    __call__ = _InstanceFunctionDescriptor()

    def __init__(self, function: Any) -> None:
        self.function = function


class ProviderWithStaticmethodCall:
    __call__ = staticmethod(other_module.get_body)


class ProviderWithClassmethodCall:
    __call__ = classmethod(other_module.provide_with_class)


class ProviderWithPartialmethodCall:
    __call__ = functools.partialmethod(other_module.provide_with_label, label="fixed")


class ProviderWithInstanceCall:
    # A callable instance without __get__ gets called as is
    __call__ = other_module.callable_provider


class ProviderWithClassCall:
    # Calling the instance constructs the class
    __call__ = other_module.Resource


class FunctionLike:
    """A duck type of a function (e.g. a function compiled with Cython) that inspect.signature() reads directly"""

    def __init__(self, function: Any) -> None:
        self.function = function
        self.__name__ = function.__name__
        self.__code__ = function.__code__
        self.__defaults__ = function.__defaults__
        self.__kwdefaults__ = function.__kwdefaults__
        self.__annotations__ = function.__annotations__
        self.__globals__ = function.__globals__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.function(*args, **kwargs)


property_provider = ProviderWithPropertyCall(other_module.get_body)
descriptor_provider = ProviderWithDescriptorCall(other_module.get_body)
staticmethod_provider = ProviderWithStaticmethodCall()
classmethod_provider = ProviderWithClassmethodCall()
partialmethod_provider = ProviderWithPartialmethodCall()
instance_call_provider = ProviderWithInstanceCall()
class_call_provider = ProviderWithClassCall()
function_like = FunctionLike(other_module.get_body)

# A partial that also carries the metadata of a function
function_like_partial = functools.partial(_invoke, other_module.get_body)
for _attribute_name in ("__name__", "__code__", "__defaults__", "__kwdefaults__", "__annotations__", "__globals__"):
    setattr(function_like_partial, _attribute_name, getattr(other_module.get_body, _attribute_name))


def test__router_generation__callable_instances_with_descriptor_call__should_resolve_annotations_like_signature(
    router, create_versioned_clients
):
    @router.post("/partialmethod")
    async def partialmethod_route(body: Annotated[Any, Depends(partialmethod_provider)]):
        return body

    paths = ["/partialmethod"]
    # Python 3.10's inspect.signature() looks up __call__ on the class instead of the instance
    # so it fails or drops the first parameter for these dependencies and even FastAPI itself cannot use them there
    if sys.version_info >= (3, 11):

        @router.post("/staticmethod")
        async def staticmethod_route(body: Annotated[Any, Depends(staticmethod_provider)]):
            return body

        @router.post("/classmethod")
        async def classmethod_route(body: Annotated[Any, Depends(classmethod_provider)]):
            return body

        @router.post("/instance")
        async def instance_route(body: Annotated[Any, Depends(instance_call_provider)]):
            return body

        @router.post("/class")
        async def class_route(resource: Annotated[Any, Depends(class_call_provider)]):
            return resource.body

        @router.post("/property")
        async def property_route(body: Annotated[Any, Depends(property_provider)]):
            return body

        @router.post("/descriptor")
        async def descriptor_route(body: Annotated[Any, Depends(descriptor_provider)]):
            return body

        paths += ["/staticmethod", "/classmethod", "/instance", "/class", "/property", "/descriptor"]
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


def test__router_generation__function_like_dependency__should_resolve_annotations_in_its_globals(
    router, create_versioned_clients
):
    @router.post("/function_like")
    async def function_like_route(body: Annotated[Any, Depends(function_like)]):
        return body

    paths = ["/function_like"]
    # Python 3.10-3.12's inspect.signature() reads a function-like partial directly. Python 3.13+ unwraps partials
    # first so it gets the signature of _invoke which has no "body" and even FastAPI itself cannot use it there
    if sys.version_info < (3, 13):

        @router.post("/function_like_partial")
        async def function_like_partial_route(body: Annotated[Any, Depends(function_like_partial)]):
            return body

        paths.append("/function_like_partial")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


def get_limit_with_type_checking_only_return(limit: int) -> Decimal:
    return limit  # ty: ignore[invalid-return-type]  # The annotation is only for type checkers.


def get_limit_with_type_checking_only_parameter(limit: int, price: Decimal | None = None) -> int:
    return limit


class LimitGetterWithTypeCheckingOnlyReturn:
    def __call__(self, limit: int) -> Decimal:
        return limit  # ty: ignore[invalid-return-type]  # The annotation is only for type checkers.


limit_getter_with_type_checking_only_return = LimitGetterWithTypeCheckingOnlyReturn()


def test__router_generation__dependency_with_type_checking_only_annotations__should_resolve_the_other_ones(
    router, create_versioned_clients
):
    @router.get("/return")
    async def return_route(limit: Annotated[int, Depends(get_limit_with_type_checking_only_return)]):
        return limit

    @router.get("/parameter")
    async def parameter_route(limit: Annotated[int, Depends(get_limit_with_type_checking_only_parameter)]):
        return limit

    @router.get("/instance")
    async def instance_route(limit: Annotated[int, Depends(limit_getter_with_type_checking_only_return)]):
        return limit

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/return", "/parameter", "/instance"):
            response = version_client.get(path, params={"limit": "5"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == 5
            assert version_client.get(path, params={"limit": "five"}).status_code == 422


class VersionedItem(BaseModel):
    foo: str


def get_item_with_type_checking_only_return(item: VersionedItem) -> Decimal:
    raise NotImplementedError


@pytest.mark.parametrize("style", ["default_dependency", "annotated_dependency"])
def test__router_generation__partial_dependency_with_type_checking_only_annotations__should_use_versioned_models(
    router, create_versioned_clients, style
):
    # FastAPI itself cannot resolve "VersionedItem" for such partials at request time (it looks for the globals on
    # the partial) so we only compare the schemas that each version documents
    if style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(
            item: Any = Depends(functools.partial(get_item_with_type_checking_only_return)),
        ):
            raise NotImplementedError

    else:

        @router.post("/test")
        async def annotated_dependency_route(
            item: Annotated[Any, Depends(functools.partial(get_item_with_type_checking_only_return))],
        ):
            raise NotImplementedError

    clients = create_versioned_clients(version_change(schema(VersionedItem).field("bar").existed_as(type=str)))
    required_fields = {}
    for version, version_client in clients.items():
        if style == "annotated_dependency" and version == "2001-01-01":
            # The latest version changes nothing that the dependency uses so, as before, it keeps the original
            # partial, whose annotation FastAPI cannot resolve
            with pytest.raises(PydanticUserError):
                version_client.get(f"/openapi.json?version={version}")
            continue
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["VersionedItem"]["required"]
    if style == "default_dependency":
        assert required_fields == {"2001-01-01": ["foo"], "2000-01-01": ["foo", "bar"]}
    else:
        assert required_fields == {"2000-01-01": ["foo", "bar"]}


class NotSubscriptableAtRuntime:
    """Like pandas.Series: its stubs make it generic for type checkers but subscripting it fails at runtime"""

    def __class_getitem__(cls, item: Any) -> Any:  # pragma: no cover # Only runs if something evaluates the annotation
        raise TypeError(f"type {cls.__name__!r} is not subscriptable")


def make_values(size: int) -> NotSubscriptableAtRuntime[int]:  # ty: ignore[invalid-type-form]  # Like pandas.
    return list(range(size))


class ModelWithCallables(BaseModel):
    values: Any = Field(default_factory=functools.partial(make_values, 3))
    size: Annotated[int, functools.partial(make_values, 2)] = 1


def test__router_generation__non_dependency_callables_with_unevaluable_annotations__should_work(
    router, create_versioned_clients
):
    # FastAPI never evaluates the annotations of callables other than endpoints and dependencies so neither does Cadwyn
    @router.post("/test")
    async def route(body: ModelWithCallables):
        return body

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.post("/test", json={}).json() == {"values": [0, 1, 2], "size": 1}


# With postponed annotations, the quotes make this a string within a string so evaluating it once leaves a string
def make_values_with_quoted_return(
    size: int = 3,
) -> "NotSubscriptableAtRuntime[int]":  # noqa: UP037  # ty: ignore[invalid-type-form]
    return list(range(size))


cached_make_values_with_quoted_return = functools.lru_cache(make_values_with_quoted_return)


def test__router_generation__dependency_with_quoted_unevaluable_return_annotation__should_work(
    router, create_versioned_clients
):
    # FastAPI never evaluates the return annotations of dependencies so neither does Cadwyn
    @router.get("/partial")
    async def partial_route(values: Annotated[Any, Depends(functools.partial(make_values_with_quoted_return, 3))]):
        return values

    @router.get("/cached")
    async def cached_route(values: Annotated[Any, Depends(cached_make_values_with_quoted_return)]):
        return values

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/partial", "/cached"):
            assert version_client.get(path).json() == [0, 1, 2], path


def test__router_generation__function_dependency_with_quoted_unevaluable_return_annotation__should_work(
    router, create_versioned_clients
):
    @router.get("/test")
    async def route(values: Annotated[Any, Depends(make_values_with_quoted_return)]):
        return values

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.get("/test").json() == [0, 1, 2]


class QuotedPayload(BaseModel):
    current: str


# With postponed annotations, the quotes make these strings within strings so evaluating them once leaves strings
def get_current_from_quoted_body(body: "QuotedPayload") -> str:  # noqa: UP037
    return body.current


@pytest.mark.parametrize("style", ["endpoint", "default_dependency", "annotated_dependency"])
def test__router_generation__quoted_parameter_annotations__should_use_versioned_models(
    router, create_versioned_clients, style
):
    if style == "endpoint":

        @router.post("/test")
        async def endpoint_route(body: "QuotedPayload"):  # noqa: UP037
            return body.current

    elif style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(current: str = Depends(get_current_from_quoted_body)):
            return current

    else:

        @router.post("/test")
        async def annotated_dependency_route(current: Annotated[str, Depends(get_current_from_quoted_body)]):
            return current

    clients = create_versioned_clients(version_change(schema(QuotedPayload).field("legacy").existed_as(type=str)))
    assert clients["2001-01-01"].post("/test", json={"current": "ok"}).json() == "ok"
    assert clients["2000-01-01"].post("/test", json={"current": "ok"}).status_code == 422
    assert clients["2000-01-01"].post("/test", json={"current": "ok", "legacy": "ok"}).json() == "ok"
    required_fields = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["QuotedPayload"]["required"]
    assert required_fields == {"2001-01-01": ["current"], "2000-01-01": ["current", "legacy"]}


# With postponed annotations, evaluating these once leaves forward references within them
def get_current_from_nested_quoted_list(items: list["QuotedPayload"]) -> str:  # noqa: UP037
    raise NotImplementedError


def get_current_from_nested_quoted_typing_list(items: typing.List["QuotedPayload"]) -> str:  # noqa: UP006, UP037
    raise NotImplementedError


def get_current_from_nested_quoted_optional(item: Optional["QuotedPayload"]) -> str:  # noqa: UP037
    raise NotImplementedError


def get_current_from_nested_quoted_dict(items: dict[str, "QuotedPayload"]) -> str:  # noqa: UP037
    raise NotImplementedError


def get_current_from_nested_quoted_annotated(items: Annotated[list["QuotedPayload"], Body()]) -> str:  # noqa: UP037
    raise NotImplementedError


_NESTED_QUOTED_DEPENDENCIES = {
    "list": (get_current_from_nested_quoted_list, lambda payload: [payload]),
    "typing_list": (get_current_from_nested_quoted_typing_list, lambda payload: [payload]),
    "optional": (get_current_from_nested_quoted_optional, lambda payload: payload),
    "dict": (get_current_from_nested_quoted_dict, lambda payload: {"key": payload}),
    "annotated": (get_current_from_nested_quoted_annotated, lambda payload: [payload]),
}


# Postponed annotations of routes can only refer to the dependency through the globals
nested_quoted_dependency: Any = None


@pytest.mark.parametrize("shape", list(_NESTED_QUOTED_DEPENDENCIES))
@pytest.mark.parametrize("style", ["endpoint", "default_dependency", "annotated_dependency"])
def test__router_generation__nested_quoted_parameter_annotations__should_use_versioned_models(
    router, create_versioned_clients, monkeypatch, style, shape
):
    if sys.version_info < (3, 11) and shape in {"list", "dict", "annotated"}:
        pytest.skip("Python 3.10 cannot evaluate forward references within builtin generics such as list['Model']")
    dependency, make_body = _NESTED_QUOTED_DEPENDENCIES[shape]
    monkeypatch.setitem(globals(), "nested_quoted_dependency", dependency)
    if style == "endpoint":
        router.post("/test")(dependency)
    elif style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(current: str = Depends(nested_quoted_dependency)):
            raise NotImplementedError

    else:

        @router.post("/test")
        async def annotated_dependency_route(current: Annotated[str, Depends(nested_quoted_dependency)]):
            raise NotImplementedError

    clients = create_versioned_clients(version_change(schema(QuotedPayload).field("legacy").existed_as(type=str)))
    # FastAPI itself cannot evaluate such forward references in the head route that Cadwyn validates requests
    # against after migrating them so we only check what each version validates and documents on its own
    assert clients["2000-01-01"].post("/test", json=make_body({"current": "ok"})).status_code == 422
    required_fields = {}
    for version, version_client in clients.items():
        if style == "annotated_dependency" and version == "2001-01-01":
            # The latest version changes nothing that the dependency uses so, as before, it keeps the original
            # dependency, whose forward refs pydantic cannot resolve
            with pytest.raises(PydanticUserError):
                version_client.get(f"/openapi.json?version={version}")
            continue
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["QuotedPayload"]["required"]
    if style == "annotated_dependency":
        assert required_fields == {"2000-01-01": ["current", "legacy"]}
    else:
        assert required_fields == {"2001-01-01": ["current"], "2000-01-01": ["current", "legacy"]}


def _with_signature_of_unevaluated_annotations(function: Any) -> Any:
    # Like decorators that rebuild the signature to inject parameters or defaults (e.g. fastapi-utils' @cbv).
    # inspect.signature() leaves postponed annotations as strings and eval_str does not evaluate __signature__
    function.__signature__ = inspect.signature(function)
    return function


@_with_signature_of_unevaluated_annotations
def get_current_from_body_with_unevaluated_signature(body: QuotedPayload) -> str:
    return body.current


@pytest.mark.parametrize("style", ["endpoint", "default_dependency", "annotated_dependency"])
def test__router_generation__signatures_with_unevaluated_annotations__should_use_versioned_models(
    router, create_versioned_clients, style
):
    if style == "endpoint":

        @router.post("/test")
        @_with_signature_of_unevaluated_annotations
        async def endpoint_route(body: QuotedPayload):
            return body.current

    elif style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(current: str = Depends(get_current_from_body_with_unevaluated_signature)):
            return current

    else:

        @router.post("/test")
        async def annotated_dependency_route(
            current: Annotated[str, Depends(get_current_from_body_with_unevaluated_signature)],
        ):
            return current

    clients = create_versioned_clients(version_change(schema(QuotedPayload).field("legacy").existed_as(type=str)))
    assert clients["2001-01-01"].post("/test", json={"current": "ok"}).json() == "ok"
    assert clients["2000-01-01"].post("/test", json={"current": "ok"}).status_code == 422
    assert clients["2000-01-01"].post("/test", json={"current": "ok", "legacy": "ok"}).json() == "ok"
    required_fields = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["QuotedPayload"]["required"]
    assert required_fields == {"2001-01-01": ["current"], "2000-01-01": ["current", "legacy"]}


def make_values_with_annotations_set_by_hand() -> Any:
    return [0, 1, 2]


make_values_with_annotations_set_by_hand.__annotations__ = {"return": "NotSubscriptableAtRuntime[int]"}


class ModelWithFactoryWithAnnotationsSetByHand(BaseModel):
    values: Any = Field(default_factory=make_values_with_annotations_set_by_hand)


def test__router_generation__default_factory_with_annotations_set_by_hand__should_read_them_like_before(
    router, create_versioned_clients
):
    @router.post("/test")
    async def route(body: ModelWithFactoryWithAnnotationsSetByHand):
        return body.values

    # Only the copies of the models read the annotations of their default factories
    model_change = version_change(schema(ModelWithFactoryWithAnnotationsSetByHand).field("values").had(description="A"))
    # Cadwyn reads the annotations of such callables through annotationlib on Python 3.14+ which ignores the ones
    # set by hand. Before 3.14, it reads __annotations__ and evaluates them
    if sys.version_info >= (3, 14):
        for version_client in create_versioned_clients(model_change).values():
            assert version_client.post("/test", json={}).json() == [0, 1, 2]
    else:
        with pytest.raises(TypeError, match="NotSubscriptableAtRuntime"):
            create_versioned_clients(model_change)


class FutureCategory(BaseModel):
    name: str
    children: list[FutureCategory] = []
    parent: Optional[FutureCategory] = None


@pytest.mark.parametrize("has_older_version", [False, True], ids=["single_version", "with_unrelated_change"])
def test__router_generation__unchanged_self_referencing_model__should_work_in_every_version(
    router, create_versioned_clients, has_older_version
):
    version_changes = (
        [version_change(schema(MySchema).field("foo").had(description="Old"))] if has_older_version else []
    )
    body = {"name": "a", "children": [{"name": "b", "children": [], "parent": None}], "parent": None}

    @router.post("/categories", response_model=FutureCategory)
    async def create_category(category: FutureCategory) -> FutureCategory:
        return category

    for version, version_client in create_versioned_clients(*version_changes).items():
        assert version_client.post("/categories", json=body).json() == body
        assert version_client.get(f"/openapi.json?version={version}").status_code == 200
