from cadwyn import VersionChange, endpoint, hidden, schema
from pydantic import BaseModel


class User(BaseModel):
    id: int


class RenameUserLookupPath(VersionChange):
    """The user lookup route is now '/users/{user_id}' instead of
    '/user/{user_id}' to match the other user routes.
    """

    instructions_to_migrate_to_previous_version = (
        hidden(
            endpoint("/users/{user_id}", ["GET"]).had(path="/user/{user_id}")
        ),
    )


@hidden
class RemoveAddressFromUser(VersionChange):
    """The legacy 'User.address' field has been removed because addresses are
    now managed as separate resources.
    """

    instructions_to_migrate_to_previous_version = (
        schema(User).field("address").existed_as(type=str),
    )
