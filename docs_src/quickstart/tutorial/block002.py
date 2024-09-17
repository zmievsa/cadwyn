import uuid

from pydantic import BaseModel, Field

from cadwyn import (
    Cadwyn,
    HeadVersion,
    Version,
    VersionBundle,
    VersionedAPIRouter,
)


class BaseUser(BaseModel):
    addresses: list[str] = Field(min_length=1)


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID


database_parody = {}
router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(payload: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {
        "id": id_,
        "addresses": payload.addresses,
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=VersionBundle(HeadVersion(), Version("2001-01-01")))
app.generate_and_include_versioned_routers(router)
