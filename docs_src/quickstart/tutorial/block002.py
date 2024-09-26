import uuid
from typing import Annotated

from pydantic import BaseModel, Field

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionedAPIRouter,
)


class UserCreateRequest(BaseModel):
    addresses: list[str]


class UserResource(BaseModel):
    id: uuid.UUID
    addresses: Annotated[list[str], Field(min_length=1)]


database_parody = {}
router = VersionedAPIRouter()


@router.post("/users")
async def create_user(payload: UserCreateRequest) -> UserResource:
    id_ = uuid.uuid4()
    database_parody[id_] = UserResource(id=id_, addresses=payload.addresses)
    return database_parody[id_]


@router.get("/users/{user_id}")
async def get_user(user_id: uuid.UUID) -> UserResource:
    return database_parody[user_id]


app = Cadwyn(versions=VersionBundle(Version("2001-01-01")))
app.generate_and_include_versioned_routers(router)
