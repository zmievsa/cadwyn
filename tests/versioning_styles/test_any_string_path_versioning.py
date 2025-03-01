import uuid

import pytest
import uvicorn
from dirty_equals import IsUUID
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from cadwyn import VersionedAPIRouter
from cadwyn.applications import Cadwyn
from cadwyn.structure import (
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
from cadwyn.structure.versions import HeadVersion


class BaseUser(BaseModel):
    pass


class UserCreateRequest(BaseUser):
    default_address: str
    addresses_to_create: list[str] = Field(default_factory=list)


class UserResource(BaseUser):
    id: uuid.UUID


class UserAddressResource(BaseModel):
    id: uuid.UUID
    value: str


class UserAddressResourceList(BaseModel):
    data: list[UserAddressResource]


class ChangeAddressToList(VersionChange):
    description = "Change vat id to list"
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("addresses").didnt_exist,
        schema(BaseUser).field("address").existed_as(type=str, info=Field()),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_address_to_multiple_items(request: RequestInfo) -> None:
        request.body["addresses"] = [request.body.pop("address")]

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_single_item(response: ResponseInfo) -> None:
        # Need to assert addresses length somewhere in business logic
        response.body["address"] = response.body["addresses"][0]


class ChangeAddressesToSubresource(VersionChange):
    description = "Change vat ids to subresource"
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("addresses").existed_as(type=list[str], info=Field()),
        schema(UserCreateRequest).field("default_address").didnt_exist,
        endpoint("/{my_api_version}/users/{user_id}/addresses", ["GET"]).didnt_exist,
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


version_bundle = VersionBundle(
    HeadVersion(RemoveAddressesToCreateFromLatest),
    Version("v10", ChangeAddressesToSubresource),
    Version("v9", ChangeAddressToList),
    Version("v8"),
)


router = VersionedAPIRouter(tags=["Users"])
database_parody = {}


@router.post("/{my_api_version}/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {"id": id_}
    addresses = create_user_addresses(id_, [user.default_address, *user.addresses_to_create])
    return database_parody[id_] | {"_prefetched_addresses": addresses}


@router.get("/{my_api_version}/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return {
        "id": user_id,
        "_prefetched_addresses": (await get_user_addresses(user_id))["data"],
    }


def create_user_addresses(user_id: uuid.UUID, addresses: list[str]):
    database_parody[f"addr_{user_id}"] = [{"id": uuid.uuid4(), "value": address} for address in addresses]
    return database_parody[f"addr_{user_id}"]


@router.get("/{my_api_version}/users/{user_id}/addresses", response_model=UserAddressResourceList)
async def get_user_addresses(user_id: uuid.UUID):
    return {"data": database_parody[f"addr_{user_id}"]}


app = Cadwyn(
    versions=version_bundle,
    title="My amazing API",
    api_version_parameter_name="my_api_version",
    api_version_style="any_string",
    api_version_location="url",
)
app.generate_and_include_versioned_routers(router)


if __name__ == "__main__":
    uvicorn.run(app)


@pytest.fixture
def testclient() -> TestClient:
    return TestClient(app)


def test__v8(testclient: TestClient):
    response = testclient.post("/v8/users", json={"address": "123 Example St"}).json()
    assert response == {
        "id": IsUUID(4),
        "address": "123 Example St",
    }
    assert testclient.get(f"/v8/users/{response['id']}").json() == {
        "id": response["id"],
        "address": "123 Example St",
    }


def test__v9(testclient: TestClient):
    response = testclient.post("/v9/users", json={"addresses": ["124", "567"]}).json()
    assert response == {
        "id": IsUUID(4),
        "addresses": ["124", "567"],
    }

    assert testclient.get(f"/v9/users/{response['id']}").json() == {
        "id": response["id"],
        "addresses": ["124", "567"],
    }


def test__v10(testclient: TestClient):
    response = testclient.post("/v10/users", json={"default_address": "wowee"}).json()

    assert response == {
        "id": IsUUID(4),
    }

    assert testclient.get(f"/v10/users/{response['id']}").json() == {"id": response["id"]}

    assert testclient.get(f"/v10/users/{response['id']}/addresses").json() == {
        "data": [
            {"id": IsUUID(4), "value": "wowee"},
        ],
    }
