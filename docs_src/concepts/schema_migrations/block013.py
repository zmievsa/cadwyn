from cadwyn import (
    Cadwyn,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    schema,
)
from pydantic import BaseModel, ConfigDict


class User(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    nickname: str = "anonymous"


class AllowExtraUserFields(VersionChange):
    """User requests and responses now allow extra fields."""

    instructions_to_migrate_to_previous_version = (
        schema(User).had(config=ConfigDict(extra="forbid")),
        schema(User).field("nickname").didnt_exist,
    )

    @convert_request_to_next_version_for(User)
    def supply_nickname(request: RequestInfo):
        request.body["nickname"] = request.body["name"]

    @convert_response_to_previous_version_for(User)
    def remove_newer_keys(response: ResponseInfo):
        del response.body["nickname"]
        del response.body["server_note"]


router = VersionedAPIRouter()


@router.post("/users")
def create_user(user: User) -> User:
    return User.model_validate(user.model_dump() | {"server_note": "created"})


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AllowExtraUserFields),
        Version("2000-01-01"),
    ),
)
app.generate_and_include_versioned_routers(router)
