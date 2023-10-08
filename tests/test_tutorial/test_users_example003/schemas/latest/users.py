from pydantic import BaseModel, Field


class UserCreateRequest(BaseModel):
    default_address: str
    _addresses_to_create: list[str] = Field(default_factory=list)


class UserResource(BaseModel):
    id: int


class UserAddressResource(BaseModel):
    id: int
    value: str


class UserAddressResourceList(BaseModel):
    data: list[UserAddressResource]
