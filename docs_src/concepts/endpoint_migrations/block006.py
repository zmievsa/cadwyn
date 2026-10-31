from cadwyn import VersionChange, endpoint


class UseParamsInsteadOfHeadersForUserNameFiltering(VersionChange):
    """'GET /users' now accepts the user-name filter as a query parameter
    instead of a header, following standard HTTP filtering conventions.
    """

    instructions_to_migrate_to_previous_version = (
        # Specify the name; otherwise you will encounter an exception due to
        # having two identical endpoints with the same parameters and path decorators
        endpoint(
            "/users",
            ["GET"],
            func_name="get_users_by_name_before_starting_to_use_params",
        ).existed,
        # Also specify the name here because, following the instructions above,
        # we now have two existing endpoints
        endpoint("/users", ["GET"], func_name="get_users_by_name").didnt_exist,
    )
