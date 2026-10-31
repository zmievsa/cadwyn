import datetime
import uuid

from cadwyn import VersionedAPIRouter
from pydantic import BaseModel


class BaseUser(BaseModel):
    date_of_birth: datetime.date


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID


ZODIAC_SIGNS_BY_START = (
    ((1, 20), "Aquarius"),
    ((2, 19), "Pisces"),
    ((3, 21), "Aries"),
    ((4, 20), "Taurus"),
    ((5, 21), "Gemini"),
    ((6, 21), "Cancer"),
    ((7, 23), "Leo"),
    ((8, 23), "Virgo"),
    ((9, 23), "Libra"),
    ((10, 23), "Scorpio"),
    ((11, 22), "Sagittarius"),
    ((12, 22), "Capricorn"),
)


def get_zodiac_sign(date_of_birth: datetime.date) -> str:
    sign = "Capricorn"
    for start, sign_starting_then in ZODIAC_SIGNS_BY_START:
        if (date_of_birth.month, date_of_birth.day) >= start:
            sign = sign_starting_then
    return sign


router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    return {
        "id": uuid.uuid4(),
        **user.model_dump(),
        # The latest version doesn't have this field but the older ones do
        "zodiac_sign": get_zodiac_sign(user.date_of_birth),
    }
