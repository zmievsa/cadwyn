import uuid

from pydantic import BaseModel

from cadwyn import (
    Cadwyn,
    RequestInfo,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    schema,
)


class UserCreateRequest(BaseModel):
    name: str
    country: str


class UserResource(BaseModel):
    id: uuid.UUID
    name: str
    country: str


class MakeUserCountryRequired(VersionChange):
    description = 'Make user country required instead of the "USA" default'
    instructions_to_migrate_to_previous_version = (
        schema(UserCreateRequest).field("country").had(default="USA"),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def add_default_value_to_country_field_in_request(request: RequestInfo):
        request.body["country"] = request.body.get("country", "USA")


version_bundle = VersionBundle(
    Version("2001-01-01", MakeUserCountryRequired),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {
        "id": id_,
        "name": user.name,
        "country": user.country,
    }
    return database_parody[id_]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
