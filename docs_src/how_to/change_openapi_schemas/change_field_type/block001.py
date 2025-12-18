import uuid
from enum import Enum

from pydantic import BaseModel

from cadwyn import (
    Cadwyn,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    convert_response_to_previous_version_for,
    enum,
)


class UserRoleEnum(str, Enum):
    admin = "admin"
    regular = "regular"
    moderator = "moderator"


class UserResource(BaseModel):
    id: uuid.UUID
    name: str
    role: UserRoleEnum


class AddModeratorRoleToUser(VersionChange):
    description = (
        "Add 'moderator' role to users that represents an admin "
        "that cannot create or remove other admins. This provides "
        "finer-grained control over permissions."
    )
    instructions_to_migrate_to_previous_version = (
        enum(UserRoleEnum).didnt_have("moderator"),
    )

    @convert_response_to_previous_version_for(UserResource)
    def change_moderator_to_regular(response: ResponseInfo):
        if response.body["role"] == "moderator":
            response.body["role"] = "regular"


version_bundle = VersionBundle(
    Version("2001-01-01", AddModeratorRoleToUser),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(name: str, role: UserRoleEnum):
    id_ = uuid.uuid4()
    database_parody[id_] = {"id": id_, "name": name, "role": role}
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
