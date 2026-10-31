from cadwyn import VersionedAPIRouter
from pydantic import BaseModel
from users import User

router = VersionedAPIRouter()


class UserFromDatabase(BaseModel):
    addresses: list[str]


database = {
    1: UserFromDatabase(addresses=["1 Main St", "2 Main St"]),
    2: UserFromDatabase(addresses=[]),
}


# in your business logic
@router.get("/users/{user_id}", response_model=User)
async def get_user(user_id: int):
    user = database[user_id]
    return {
        "address": user.addresses[0] if user.addresses else None,
        **user.model_dump(),
    }
