import uuid

from pydantic import BaseModel

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionedAPIRouter,
)


class UserCreateRequest(BaseModel):
    address: str


class UserResource(BaseModel):
    id: uuid.UUID
    address: str


database_parody = {}
router = VersionedAPIRouter()


@router.post("/users")
async def create_user(payload: UserCreateRequest) -> UserResource:
    id_ = uuid.uuid4()
    database_parody[id_] = UserResource(id=id_, address=payload.address)
    return database_parody[id_]


@router.get("/users/{user_id}")
async def get_user(user_id: uuid.UUID) -> UserResource:
    return database_parody[user_id]


app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))
app.generate_and_include_versioned_routers(router)
