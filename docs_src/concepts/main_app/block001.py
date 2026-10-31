from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter

my_version_bundle = VersionBundle(Version("2022-11-16"))
router = VersionedAPIRouter()


@router.get("/users/", tags=["users"])
async def read_users():
    return [{"username": "Rick"}, {"username": "Morty"}]


@router.get("/users/{username}", tags=["users"])
async def read_user(username: str):
    return {"username": username}


app = Cadwyn(versions=my_version_bundle)
app.generate_and_include_versioned_routers(router)
