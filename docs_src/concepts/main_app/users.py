"""The users that the fastapi-pagination examples of the page paginate."""

from pydantic import BaseModel


class User(BaseModel):
    id: int
    name: str


def get_users() -> list[User]:
    return [User(id=1, name="Rick"), User(id=2, name="Morty")]
