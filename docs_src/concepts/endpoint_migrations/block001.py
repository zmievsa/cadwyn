from cadwyn import VersionedAPIRouter

router = VersionedAPIRouter()


@router.only_exists_in_older_versions
@router.get("/users/{user_id}")
async def my_old_endpoint(): ...
