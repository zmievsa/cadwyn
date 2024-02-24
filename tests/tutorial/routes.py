import uuid
from typing import Annotated

from cadwyn import VersionedAPIRouter
from cadwyn.applications import Cadwyn
from cadwyn.routing import InternalRepresentationOf

from .data.latest.users import (
    UserAddressResourceList,
    UserCreateRequest,
    UserResource,
)
from .data.unversioned import InternalUserCreateRequest
from .versions import version_bundle

router = VersionedAPIRouter()
database_parody = {}


@router.post("/users", response_model=UserResource)
async def create_user(user: Annotated[InternalUserCreateRequest, InternalRepresentationOf[UserCreateRequest]]):
    id_ = uuid.uuid4()
    database_parody[id_] = {"id": id_}
    addresses = create_user_addresses(id_, [user.default_address, *user.addresses_to_create])
    return database_parody[id_] | {"_prefetched_addresses": addresses}


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return {
        "id": user_id,
        "_prefetched_addresses": (await get_user_addresses(user_id))["data"],
    }


def create_user_addresses(user_id: uuid.UUID, addresses: list[str]):
    database_parody[f"addr_{user_id}"] = [{"id": uuid.uuid4(), "value": address} for address in addresses]
    return database_parody[f"addr_{user_id}"]


@router.get("/users/{user_id}/addresses", response_model=UserAddressResourceList)
async def get_user_addresses(user_id: uuid.UUID):
    return {"data": database_parody[f"addr_{user_id}"]}


app = Cadwyn(versions=version_bundle)
