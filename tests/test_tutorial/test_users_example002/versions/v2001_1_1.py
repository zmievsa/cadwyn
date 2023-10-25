from pydantic import Field

from cadwyn.structure import (
    RequestInfo,
    ResponseInfo,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    schema,
)
from tests.test_tutorial.test_users_example002.data.latest.users import UserCreateRequest, UserResource


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
