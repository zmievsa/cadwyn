from pydantic import BaseModel


class UserCreateRequest(BaseModel):
    full_name: str


class UserResource(BaseModel):
    id: int
    full_name: str
