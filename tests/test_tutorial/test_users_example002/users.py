from datetime import date
from typing import Any

from universi import Field, VersionedAPIRouter
from universi.structure import (
    Version,
    VersionBundle,
    VersionChange,
    convert_response_to_previous_version_for,
    endpoint,
    schema,
)

from .schemas.latest.users import (
    UserAddressResourceList,
    UserCreateRequest,
    UserResource,
)

router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    return {
        "id": 83,
        "_prefetched_addresses": [{"id": 100, "value": user.default_address}],
    }


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: int):
    return {
        "id": user_id,
        "_prefetched_addresses": (await get_user_addresses(user_id))["data"],
    }


@router.get("/users/{user_id}/addresses", response_model=UserAddressResourceList)
async def get_user_addresses(user_id: int):
    return {
        "data": [
            {"id": 83, "value": "123 Example St"},
            {"id": 91, "value": "456 Main St"},
        ],
    }


class ChangeAddressToList(VersionChange):
    description = "Change vat id to list"
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("addresses").didnt_exist,
        schema(UserCreateRequest).field("address").existed_with(type=str, info=Field()),
        schema(UserResource).field("addresses").didnt_exist,
        schema(UserResource).field("address").existed_with(type=str, info=Field()),
    )

    @convert_response_to_previous_version_for(get_user, create_user)
    def change_addresses_to_single_item(cls, data: dict[str, Any]) -> None:
        data["address"] = data.pop("addresses")[0]

    @schema(UserCreateRequest).had_property("addresses")
    def addresses_property(parsed_schema):
        return [parsed_schema.address]  # pragma: no cover


class ChangeAddressesToSubresource(VersionChange):
    description = "Change vat ids to subresource"
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest)
        .field("addresses")
        .existed_with(type=list[str], info=Field()),
        schema(UserCreateRequest).field("default_address").didnt_exist,
        schema(UserResource)
        .field("addresses")
        .existed_with(type=list[str], info=Field()),
        endpoint(get_user_addresses).didnt_exist,
    )

    @convert_response_to_previous_version_for(get_user, create_user)
    def change_addresses_to_list(cls, data: dict[str, Any]) -> None:
        data["addresses"] = [id["value"] for id in data.pop("_prefetched_addresses")]

    @schema(UserCreateRequest).had_property("default_address")
    def default_address_property(parsed_schema):
        return parsed_schema.addresses[0]  # pragma: no cover


versions = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressesToSubresource),
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
)
