import uuid

from pydantic import BaseModel

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


class BaseUser(BaseModel):
    bio: str


class UserCreateRequest(BaseUser):
    pass


class UserResource(BaseUser):
    id: uuid.UUID


class RenameSummaryIntoBioInUser(VersionChange):
    description = (
        "Rename 'summary' field into 'bio' to keep up with industry standards"
    )
    instructions_to_migrate_to_previous_version = (
        schema(BaseUser).field("bio").had(name="summary"),
    )

    @convert_request_to_next_version_for(UserCreateRequest)
    def rename_summary_to_bio_in_request(request: RequestInfo):
        request.body["bio"] = request.body.pop("summary")

    @convert_response_to_previous_version_for(UserResource)
    def rename_bio_to_summary_in_response(response: ResponseInfo):
        response.body["summary"] = response.body.pop("bio")


version_bundle = VersionBundle(
    Version("2001-01-01", RenameSummaryIntoBioInUser),
    Version("2000-01-01"),
)

router = VersionedAPIRouter()

database_parody: dict[uuid.UUID, dict] = {}


@router.post("/users", response_model=UserResource)
async def create_user(user: UserCreateRequest):
    id_ = uuid.uuid4()
    database_parody[id_] = {"id": id_, "bio": user.bio}
    return database_parody[id_]


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: uuid.UUID):
    return database_parody[user_id]


app = Cadwyn(versions=version_bundle)
app.generate_and_include_versioned_routers(router)
