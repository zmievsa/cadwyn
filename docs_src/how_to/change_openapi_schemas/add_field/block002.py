import uuid
from typing import Optional

from pydantic import BaseModel

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)
from cadwyn.structure.versions import HeadVersion


class UserCreateRequest(BaseModel):
    name: str
    phone: Optional[str] = None


class UserResource(BaseModel):
    id: uuid.UUID
    name: str
    phone: Optional[str] = None


class MakePhoneNonNullableInLatest(VersionChange):
    description = (
        "Make sure the phone is nullable in the HEAD version to support "
        "versions older than 2001_01_01 where it became non-nullable"
    )
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("phone").had(type=str),
        schema(UserCreateRequest).field("phone").didnt_have("default"),
    )


class AddPhoneToUser(VersionChange):
    description = (
        "Add a required phone field to User to allow us to do 2fa and to "
        "make it possible to verify new user accounts using an sms."
    )
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest)
        .field("phone")
        .had(type=Optional[str], default=None),
    )


version_bundle = VersionBundle(
    HeadVersion(MakePhoneNonNullableInLatest),
    Version("2001-01-01", AddPhoneToUser),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {"id": id_, "name": user.name, "phone": user.phone}
    return database_parody[id_]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
