from cadwyn import VersionChange, endpoint


class RemoveGetUserByIdEndpoint(VersionChange):
    """The 'GET /users/{user_id}' endpoint has been removed because user
    profiles are now retrieved through the users collection.
    """

    instructions_to_migrate_to_previous_version = (
        endpoint("/users/{user_id}", ["GET"]).existed,
    )
