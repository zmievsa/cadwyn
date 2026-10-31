import uuid

from cadwyn import VersionedAPIRouter
from pydantic import BaseModel


class BaseUser(BaseModel):
    name: str


class UserCreateRequest(BaseUser):
    country: str


class UserResource(BaseUser):
    id: uuid.UUID
    country: str


router = VersionedAPIRouter()


@router.post("/users")
async def create_user(user: UserCreateRequest) -> UserResource:
    return UserResource(id=uuid.uuid4(), **user.model_dump())
