import uuid

from cadwyn import VersionedAPIRouter
from pydantic import BaseModel


class BaseUser(BaseModel):
    name: str
    phone: str | None = None


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID
    phone: str | None = None


database_parody: dict[uuid.UUID, UserResource] = {}
router = VersionedAPIRouter()


@router.post("/users")
async def create_user(user: UserCreateRequest) -> UserResource:
    id_ = uuid.uuid4()
    database_parody[id_] = UserResource(id=id_, **user.model_dump())
    return database_parody[id_]


@router.get("/users/{user_id}")
async def get_user(user_id: uuid.UUID) -> UserResource:
    return database_parody[user_id]
