import uuid

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)
from cadwyn.structure.versions import HeadVersion
from pydantic import BaseModel


class UserCreateRequest(BaseModel):
    name: str


class UserResource(BaseModel):
    id: uuid.UUID
    name: str


class AddLengthConstraintToNameInLatest(VersionChange):
    description = (
        "Remove the max_length constraint from the HEAD version to support "
        "versions older than 2001_01_01 where it did not have the constraint."
    )
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("name").had(max_length=250),
    )


class AddMaxLengthConstraintToUserNames(VersionChange):
    description = (
        "Add a max length of 250 to user names when creating new users "
        "to prevent overly large names from being used."
    )
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("name").didnt_have("max_length"),
    )


version_bundle = VersionBundle(
    HeadVersion(AddLengthConstraintToNameInLatest),
    Version("2001-01-01", AddMaxLengthConstraintToUserNames),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {"id": id_, "name": user.name}
    return database_parody[id_]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
