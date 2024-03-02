from pydantic import Field

from cadwyn.structure import (
    RequestInfo,
    ResponseInfo,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    endpoint,
    schema,
)
from tests.tutorial.data.head.users import BaseUser, UserCreateRequest, UserResource


class ChangeAddressesToSubresource(VersionChange):
    description = "Change vat ids to subresource"
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("addresses").existed_as(type=list[str], info=Field()),
        schema(UserCreateRequest).field("default_address").didnt_exist,
        endpoint("/users/{user_id}/addresses", ["GET"]).didnt_exist,
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_addresses_to_default_address(request: RequestInfo):
        request.body["default_address"] = request.body["addresses"].pop(0)
        # Save data to still be able to keep the old behavior of creating addresses
        request.body["addresses_to_create"] = request.body.pop("addresses")

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_list(response: ResponseInfo) -> None:
        response.body["addresses"] = [id["value"] for id in response.body["_prefetched_addresses"]]


class RemoveAddressesToCreateFromLatest(VersionChange):
    description = (
        "In order to support old versions, we gotta have `addresses_to_create` located in "
        "head schemas but we do not need this field in latest schemas."
    )
    instructions_to_migrate_to_previous_version = (schema(UserCreateRequest).field("addresses_to_create").didnt_exist,)
