from contextvars import ContextVar
from datetime import date

from pydantic import Field

from universi import VersionedAPIRouter
from universi.structure import (
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    endpoint,
    schema,
)

from .schemas.latest.users import (
    UserAddressResourceList,
    UserCreateRequest,
    UserResource,
)

api_version_var = ContextVar("api_version_var")
router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    return {
        "id": 83,
        "_prefetched_addresses": [
            {"id": i, "value": address} for i, address in enumerate([user.default_address, *user._addresses_to_create])
        ],
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
        schema(UserCreateRequest).field("address").existed_as(type=str, info=Field()),
        schema(UserResource).field("addresses").didnt_exist,
        schema(UserResource).field("address").existed_as(type=str, info=Field()),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_address_to_multiple_items(request: RequestInfo):
        request.body["addresses"] = [request.body.pop("address")]

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_single_item(response: ResponseInfo) -> None:
        # Need to assert addresses length somewhere in business logic
        response.body["address"] = response.body["addresses"][0]


class ChangeAddressesToSubresource(VersionChange):
    description = "Change vat ids to subresource"
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("addresses").existed_as(type=list[str], info=Field()),
        schema(UserCreateRequest).field("default_address").didnt_exist,
        schema(UserCreateRequest).field("_addresses_to_create").didnt_exist,
        schema(UserResource).field("addresses").existed_as(type=list[str], info=Field()),
        endpoint("/users/{user_id}/addresses", ["GET"]).didnt_exist,
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_addresses_to_default_address(request: RequestInfo):
        request.body["default_address"] = request.body["addresses"].pop(0)
        # Save data to still be able to keep the old behavior of creating addresses
        request.body["_addresses_to_create"] = request.body.pop("addresses")

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_list(response: ResponseInfo) -> None:
        response.body["addresses"] = [id["value"] for id in response.body["_prefetched_addresses"]]


versions = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressesToSubresource),
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
    api_version_var=api_version_var,
)
