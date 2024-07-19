import datetime

from pydantic import BaseModel, Field

from cadwyn import (
    HeadVersion,
    Version,
    VersionBundle,
    VersionChange,
    endpoint,
    schema,
)
from cadwyn.changelogs import ChangelogEntryType, generate_changelog


# TODO: Add tests with schema and field renamings
def test__changelog():
    class BaseUser(BaseModel):
        pass

    class UserCreateRequest(BaseUser):
        default_address: str
        addresses_to_create: list[str] = Field(default_factory=list)

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

    assert generate_changelog(version_bundle).model_dump(mode="json") == {
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
                                "schema_": "BaseUser",
                                "field": "addresses",
                            },
                            {
                                "type": ChangelogEntryType.schema_field_added,
                                "schema_": "UserCreateRequest",
                                "field": "default_address",
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
                                "schema_": "BaseUser",
                                "field": "addresses",
                            },
                            {
                                "type": ChangelogEntryType.schema_field_removed,
                                "schema_": "BaseUser",
                                "field": "address",
                            },
                        ],
                    }
                ],
            },
            {"value": "2000-01-01", "changes": []},
        ]
    }
