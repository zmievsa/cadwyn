import uuid

from pydantic import BaseModel, Field


class BaseUser(BaseModel):
    pass


class UserCreateRequest(BaseUser):
    default_address: str
    addresses_to_create: list[str] = Field(default_factory=list)


class UserResource(BaseUser):
    id: uuid.UUID


class UserAddressResource(BaseModel):
    id: uuid.UUID
    value: str


class UserAddressResourceList(BaseModel):
    data: list[UserAddressResource]
