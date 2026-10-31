from pydantic import BaseModel


class User(BaseModel):
    """A user of the API.

    Internal: the support team can merge duplicate users by email.
    """

    name: str
