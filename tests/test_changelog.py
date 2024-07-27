import datetime
import uuid
from enum import Enum, IntEnum, auto

from dirty_equals import IsList
from pydantic import BaseModel, Field

from cadwyn import (
    HeadVersion,
    Version,
    VersionBundle,
    VersionChange,
    endpoint,
    schema,
)
from cadwyn.applications import Cadwyn
from cadwyn.changelogs import ChangelogEntryType, StrEnum, generate_changelog
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.enums import enum
from tests.conftest import version_change


def unordered(*items):
    return IsList(*items, check_order=False)


# TODO: Add tests with schema and field renamings
def test__changelog__with_multiple_versions():
    class BaseUser(BaseModel):
        pass

    class UserCreateRequest(BaseUser):
        default_address: str
        addresses_to_create: list[str] = Field(default_factory=list)

    class UserUpdateRequest(BaseUser):
        pass

    class UserResource(BaseUser):
        id: uuid.UUID

    class UserAddressResource(BaseModel):
        id: uuid.UUID
        value: list[str | dict[str, UserResource]]

    class UserAddressResourceList(BaseModel):
        data: list[UserAddressResource]

    class ChangeAddressToList(VersionChange):
        description = "Change vat id to list"
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser).field("addresses").didnt_exist,
            schema(BaseUser).field("address").existed_as(type=str, info=Field()),
        )

    class ChangeAddressesToSubresource(VersionChange):
        description = "Change vat ids to subresource"
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser).field("addresses").existed_as(type=list[str], info=Field()),
            schema(UserCreateRequest).field("default_address").didnt_exist,
            endpoint("/users/{user_id}/addresses", ["GET"]).didnt_exist,
        )

    class RemoveAddressesToCreateFromLatest(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("addresses_to_create").didnt_exist,
        )

    version_bundle = VersionBundle(
        HeadVersion(RemoveAddressesToCreateFromLatest),
        Version(datetime.date(2002, 1, 1), ChangeAddressesToSubresource),
        Version(datetime.date(2001, 1, 1), ChangeAddressToList),
        Version(datetime.date(2000, 1, 1)),
    )

    router = VersionedAPIRouter(tags=["Users"])

    @router.post("/users", response_model=UserResource)
    async def create_user(user: UserCreateRequest): ...
    @router.patch("/users", response_model=UserResource)
    async def patch_user(user: list[UserUpdateRequest | None]): ...
    @router.get("/users/{user_id}", response_model=UserResource)
    async def get_user(user_id: uuid.UUID): ...

    @router.get("/users/{user_id}/addresses", response_model=UserAddressResourceList)
    async def get_user_addresses(user_id: uuid.UUID): ...

    app = Cadwyn(versions=version_bundle)
    app.generate_and_include_versioned_routers(router)

    assert generate_changelog(app).model_dump(mode="json") == {
        "versions": [
            {
                "value": "2002-01-01",
                "changes": [
                    {
                        "description": "Change vat ids to subresource",
                        "side_effects": False,
                        "instructions": [
                            {
                                "type": ChangelogEntryType.endpoint_added,
                                "path": "/users/{user_id}/addresses",
                                "methods": ["GET"],
                            },
                            {
                                "type": ChangelogEntryType.schema_field_removed,
                                "models": unordered("UserCreateRequest", "UserResource", "UserUpdateRequest"),
                                "field": "addresses",
                            },
                            {
                                "type": ChangelogEntryType.schema_field_added,
                                "models": ["UserCreateRequest"],
                                "field": "default_address",
                                "field_info": {"title": "Default Address", "type": "string"},
                            },
                        ],
                    }
                ],
            },
            {
                "value": "2001-01-01",
                "changes": [
                    {
                        "description": "Change vat id to list",
                        "side_effects": False,
                        "instructions": [
                            {
                                "type": ChangelogEntryType.schema_field_added,
                                "models": unordered("UserCreateRequest", "UserUpdateRequest", "UserResource"),
                                "field": "addresses",
                                "field_info": {"items": {"type": "string"}, "title": "Addresses", "type": "array"},
                            },
                            {
                                "type": ChangelogEntryType.schema_field_removed,
                                "models": unordered("UserCreateRequest", "UserUpdateRequest", "UserResource"),
                                "field": "address",
                            },
                        ],
                    }
                ],
            },
        ]
    }


def test__changelog__enum_interactions():
    class MyIntEnum(IntEnum):
        a = 83
        b = auto()

    class MyStrEnum(StrEnum):
        a = "hewwo"
        b = auto()

    version_change_1 = version_change(
        enum(MyIntEnum).didnt_have("a", "b"),
        enum(MyIntEnum).had(c=11, d=auto()),
        enum(MyStrEnum).didnt_have("a", "b"),
        enum(MyStrEnum).had(c="11", d=auto()),
    )

    version_bundle = VersionBundle(
        Version(datetime.date(2001, 1, 1), version_change_1),
        Version(datetime.date(2000, 1, 1)),
    )
    app = Cadwyn(versions=version_bundle)
    app.generate_and_include_versioned_routers()

    assert generate_changelog(app).model_dump(mode="json")["versions"][0]["changes"][0]["instructions"] == [
        {
            "type": ChangelogEntryType.enum_members_added,
            "enum": "MyIntEnum",
            "members": [{"name": "a", "value": 83}, {"name": "b", "value": 84}],
        },
        {
            "type": ChangelogEntryType.enum_members_removed,
            "enum": "MyIntEnum",
            "member_changes": [
                {"name": "c", "status": "removed", "old_value": 11, "new_value": None},
                {"name": "d", "status": "removed", "old_value": 12, "new_value": None},
            ],
        },
        {
            "type": ChangelogEntryType.enum_members_added,
            "enum": "MyStrEnum",
            "members": [{"name": "a", "value": "hewwo"}, {"name": "b", "value": "b"}],
        },
        {
            "type": ChangelogEntryType.enum_members_removed,
            "enum": "MyStrEnum",
            "member_changes": [
                {"name": "c", "status": "removed", "old_value": "11", "new_value": None},
                {"name": "d", "status": "removed", "old_value": "d", "new_value": None},
            ],
        },
    ]


def test__changelog__basic_schema_interactions():
    class SchemaWithSomeField(BaseModel):
        some_field: str = Field(pattern="sasdasd")

    # TODO: Test a case where child overrides the field
    class SchemaChild(SchemaWithSomeField):
        pass

    version_change_1 = version_change(
        schema(SchemaWithSomeField).field("some_field").didnt_have("pattern"),
        schema(SchemaWithSomeField).field("some_field").had(min_length=30, deprecated=True),
        schema(SchemaWithSomeField).field("some_field").had(max_length=50),
    )

    version_bundle = VersionBundle(
        Version(datetime.date(2001, 1, 1), version_change_1),
        Version(datetime.date(2000, 1, 1)),
    )
    app = Cadwyn(versions=version_bundle)

    router = VersionedAPIRouter()

    @router.post("/route1")
    async def route1(user: SchemaWithSomeField | str): ...

    app.generate_and_include_versioned_routers(router)

    assert generate_changelog(app).model_dump(mode="json")["versions"][0]["changes"][0]["instructions"] == [
        {
            "type": ChangelogEntryType.schema_field_attributes_changed,
            "models": ["SchemaWithSomeField"],
            "field": "some_field",
            "attribute_changes": [{"name": "pattern", "status": "added", "old_value": None, "new_value": "sasdasd"}],
        },
        {
            "type": ChangelogEntryType.schema_field_attributes_changed,
            "models": ["SchemaWithSomeField"],
            "field": "some_field",
            "attribute_changes": [
                {"name": "deprecated", "status": "removed", "old_value": True, "new_value": None},
                {"name": "minLength", "status": "removed", "old_value": 30, "new_value": None},
            ],
        },
        {
            "type": ChangelogEntryType.schema_field_attributes_changed,
            "models": ["SchemaWithSomeField"],
            "field": "some_field",
            "attribute_changes": [{"name": "maxLength", "status": "removed", "old_value": 50, "new_value": None}],
        },
    ]


def test__changelog__basic_endpoint_interactions():
    router = VersionedAPIRouter()

    class MyResponseModel(BaseModel):
        a: str

    @router.post("/route1", response_model=MyResponseModel)
    async def route1(): ...

    @router.get("/route1", response_model=MyResponseModel)
    async def route1_get(): ...

    version_change_1 = version_change(
        endpoint("/route1", ["POST", "GET"]).had(
            path="/hello/",
            response_model=None,
            status_code=201,
            tags=["1", "2"],
            summary="wewe",
        ),
    )

    version_bundle = VersionBundle(
        Version(datetime.date(2001, 1, 1), version_change_1),
        Version(datetime.date(2000, 1, 1)),
    )
    app = Cadwyn(versions=version_bundle)
    app.generate_and_include_versioned_routers(router)

    assert generate_changelog(app).model_dump(mode="json")["versions"][0]["changes"][0]["instructions"] == [
        {
            "type": ChangelogEntryType.endpoint_changed,
            "path": "/route1",
            "methods": unordered("GET", "POST"),
            "changes": [
                {"name": "path", "new_value": "/hello/"},
                {"name": "summary", "new_value": "wewe"},
                {"name": "tags", "new_value": ["1", "2"]},
                {
                    "name": "responses",
                    "new_value": {
                        "post": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/MyResponseModel"}}
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}
                                },
                            },
                        },
                        "get": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/MyResponseModel"}}
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}
                                },
                            },
                        },
                    },
                },
            ],
        }
    ]
