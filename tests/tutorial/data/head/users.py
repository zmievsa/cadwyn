import uuid

from pydantic import BaseModel


class BaseUser(BaseModel):
    pass


class UserCreateRequest(BaseUser):
    default_address: str


class UserResource(BaseUser):
    id: uuid.UUID


class UserAddressResource(BaseModel):
    id: uuid.UUID
    value: str


class UserAddressResourceList(BaseModel):
    data: list[UserAddressResource]
