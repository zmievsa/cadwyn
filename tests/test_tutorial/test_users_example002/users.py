from typing import cast

from cadwyn import VersionedAPIRouter
from cadwyn.main import _Cadwyn

from .schemas import latest
from .schemas.latest.users import (
    UserAddressResourceList,
    UserCreateRequest,
    UserResource,
)
from .schemas.unversioned import InternalUserCreateRequest
from .versions import version_bundle

router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    user = cast(InternalUserCreateRequest, user)
    return {
        "id": 83,
        "_prefetched_addresses": [
            {"id": i, "value": address} for i, address in enumerate([user.default_address, *user.addresses_to_create])
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


app = _Cadwyn(latest_schemas_module=latest, versions=version_bundle)
