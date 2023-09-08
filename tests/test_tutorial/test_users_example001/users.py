from datetime import date
from typing import Any

from universi import VersionedAPIRouter
from pydantic import Field
from universi.structure import (
    Version,
    VersionBundle,
    VersionChange,
    convert_response_to_previous_version_for,
    schema,
)

from .schemas.latest.users import (
    UserCreateRequest,
    UserResource,
)

router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(payload: UserCreateRequest):
    return {
        "id": 83,
        "addresses": payload.addresses,
    }


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: int):
    return {
        "id": user_id,
        "addresses": ["123 Example St", "456 Main St"],
    }


class ChangeAddressToList(VersionChange):
    description = "Change user address to a list of strings to allow the user to specify multiple addresses"
    instructions_to_migrate_to_previous_version = (
        # You should use schema inheritance if you don't want to repeat yourself in such cases
        schema(UserCreateRequest).field("addresses").didnt_exist,
        schema(UserCreateRequest).field("address").existed_with(type=str, info=Field()),
        schema(UserResource).field("addresses").didnt_exist,
        schema(UserResource).field("address").existed_with(type=str, info=Field()),
    )

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_single_item(cls, data: dict[str, Any]) -> None:
        data["address"] = data.pop("addresses")[0]

    @schema(UserCreateRequest).had_property("addresses")
    def addresses_property(parsed_schema):
        return [parsed_schema.address]  # pragma: no cover


versions = VersionBundle(
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
)
