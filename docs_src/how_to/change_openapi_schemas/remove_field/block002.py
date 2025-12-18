import uuid
from typing import Optional

from pydantic import BaseModel, Field

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)
from cadwyn.structure.versions import HeadVersion


class BaseUser(BaseModel):
    name: str
    middle_name: Optional[str] = None


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID


class RemoveMiddleNameFromLatestVersion(VersionChange):
    description = (
        "Remove 'User.middle_name' from latest but keep it in HEAD "
        "to support versions before 2001-01-01."
    )
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("middle_name").didnt_exist,
    )


class RemoveMiddleNameFromUser(VersionChange):
    description = "Remove 'User.middle_name' field"
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser)
        .field("middle_name")
        .existed_as(
            type=Optional[str],
            info=Field(description="User's Middle Name", default=None),
        ),
    )


version_bundle = VersionBundle(
    HeadVersion(RemoveMiddleNameFromLatestVersion),
    Version("2001-01-01", RemoveMiddleNameFromUser),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {
        "id": id_,
        "name": user.name,
        "middle_name": user.middle_name,
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
