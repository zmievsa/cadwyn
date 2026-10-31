from cadwyn import VersionChange, endpoint


class ChangeGetUserByIdEndpointDescription(VersionChange):
    """The 'GET /users/{user_id}' documentation now clarifies the returned
    user data so clients can interpret the response correctly.
    """

    instructions_to_migrate_to_previous_version = (
        endpoint("/users/{user_id}", ["GET"]).had(
            description="My old description",
        ),
    )
