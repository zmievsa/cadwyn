import datetime
import uuid
from typing import Union

from pydantic import BaseModel, field_validator

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)
from cadwyn.structure.versions import HeadVersion


@field_validator("date_of_birth", mode="before")
def convert_date_of_birth_to_date(
    cls, v: Union[datetime.date, datetime.datetime]
):
    if isinstance(v, datetime.datetime):
        return v.date()
    return v


class BaseUser(BaseModel):
    date_of_birth: datetime.datetime


class UserCreateRequest(BaseUser):
    name: str


class UserResource(BaseUser):
    id: uuid.UUID
    name: str


class ChangeDateOfBirthToDateInUserInLatest(VersionChange):
    description = (
        "Change 'BaseUser.date_of_birth' field type to datetime in HEAD "
        "to support versions and data before 2001-01-01. "
    )
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("date_of_birth").had(type=datetime.date),
        # This step is only necessary in Pydantic 2 because datetime
        # won't be converted to date automatically.
        schema(BaseUser).validator(convert_date_of_birth_to_date).existed,
    )


class ChangeDateOfBirthToDateInUser(VersionChange):
    description = (
        "Change 'User.date_of_birth' field type to date instead of "
        "a datetime because storing the exact time is unnecessary."
    )
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("date_of_birth").had(type=datetime.datetime),
        schema(BaseUser).validator(convert_date_of_birth_to_date).didnt_exist,
    )


version_bundle = VersionBundle(
    HeadVersion(ChangeDateOfBirthToDateInUserInLatest),
    Version("2001-01-01", ChangeDateOfBirthToDateInUser),
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
        "date_of_birth": user.date_of_birth,
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
