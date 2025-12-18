import datetime
import uuid

from pydantic import BaseModel, Field

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)


class UserResource(BaseModel):
    id: uuid.UUID
    date_of_birth: datetime.date


def get_zodiac_sign(date_of_birth: datetime.date) -> str:
    month, day = date_of_birth.month, date_of_birth.day
    if (month == 3 and day >= 21) or (month == 4 and day <= 19):
        return "aries"
    elif (month == 4 and day >= 20) or (month == 5 and day <= 20):
        return "taurus"
    elif (month == 5 and day >= 21) or (month == 6 and day <= 20):
        return "gemini"
    elif (month == 6 and day >= 21) or (month == 7 and day <= 22):
        return "cancer"
    elif (month == 7 and day >= 23) or (month == 8 and day <= 22):
        return "leo"
    elif (month == 8 and day >= 23) or (month == 9 and day <= 22):
        return "virgo"
    elif (month == 9 and day >= 23) or (month == 10 and day <= 22):
        return "libra"
    elif (month == 10 and day >= 23) or (month == 11 and day <= 21):
        return "scorpio"
    elif (month == 11 and day >= 22) or (month == 12 and day <= 21):
        return "sagittarius"
    elif (month == 12 and day >= 22) or (month == 1 and day <= 19):
        return "capricorn"
    elif (month == 1 and day >= 20) or (month == 2 and day <= 18):
        return "aquarius"
    else:
        return "pisces"


class RemoveZodiacSignFromUser(VersionChange):
    description = (
        "Remove 'zodiac_sign' field from UserResource because "
        "it can be inferred from user's date of birth and because "
        "only a small number of users has utilized it."
    )
    instructions_to_migrate_to_previous_version = (
        schema(UserResource)
        .field("zodiac_sign")
        .existed_as(type=str, info=Field(description="User's magical sign")),
    )


version_bundle = VersionBundle(
    Version("2001-01-01", RemoveZodiacSignFromUser),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(date_of_birth: datetime.date):
    id_ = uuid.uuid4()
    database_parody[id_] = {
        "id": id_,
        "date_of_birth": date_of_birth,
        "zodiac_sign": get_zodiac_sign(date_of_birth),
    }
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
