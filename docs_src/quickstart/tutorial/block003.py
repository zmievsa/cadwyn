import uuid

from pydantic import BaseModel, Field

from cadwyn import (
    Cadwyn,
    HeadVersion,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    schema,
)


class BaseUser(BaseModel):
    addresses: list[str] = Field(min_length=1)


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID


database_parody = {}
router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(payload: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {
        "id": id_,
        "addresses": payload.addresses,
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


class ChangeAddressToList(VersionChange):
    description = "Give user the ability to have multiple addresses at the same time"
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("addresses").didnt_exist,
        schema(BaseUser).field("address").existed_as(type=str, info=Field()),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_address_to_multiple_items(request: RequestInfo):
        request.body["addresses"] = [request.body.pop("address")]

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_single_item(response: ResponseInfo) -> None:
        response.body["address"] = response.body.pop("addresses")[0]


app = Cadwyn(
    versions=VersionBundle(
        HeadVersion(),
        Version("2001-01-01", ChangeAddressToList),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
