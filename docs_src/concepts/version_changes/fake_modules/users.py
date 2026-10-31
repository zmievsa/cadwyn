from pydantic import BaseModel


class BaseUser(BaseModel):
    address: str | None


class User(BaseModel):
    address: str | None


class UserResource(BaseModel):
    name: str
