from pydantic import BaseModel


class UserCreateRequest(BaseModel):
    default_address: str


class UserResource(BaseModel):
    id: int


class UserAddressResource(BaseModel):
    id: int
    value: str


class UserAddressResourceList(BaseModel):
    data: list[UserAddressResource]
