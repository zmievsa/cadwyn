
# Change endpoints in a new version

## Add a new endpoint

It is not a breaking change so it's recommended to simply add it to all versions. If you believe that you still need it, you can use the [following migration](../../concepts/endpoint_migrations.md#defining-endpoints-that-didnt-exist-in-old-versions).

## Delete an old endpoint

See [concepts](../../concepts/endpoint_migrations.md#defining-endpoints-that-didnt-exist-in-new-versions)

## Change an attribute of an endpoint

Changing a "decoratory" attribute such as description or summary is generally not a breaking change and should just be applied to all versions. However, if such a change is required, you can just follow the instructions in the respective [concepts docs](../../concepts/endpoint_migrations.md#changing-endpoint-attributes).

There are, however, attributes that can significantly affect our end user -- for example,

This is not a breaking change in terms of requests but it [**can be**](#why-enum-expansion-is-a-breaking-change-for-responses) a breaking change in terms of responses.

So if you do consider it a breaking change in terms of responses, you should do the following:

1. Add `moderator` value into `data.head.users.BaseUserRoleEnum`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import VersionChange, endpoint


    class AddModeratorRoleToUser(VersionChange):
        description = (
            "Add 'moderator' role to users that represents an admin that "
            "cannot create or remove other admins. This allows for a "
            "finer-grained permission control."
        )
        instructions_to_migrate_to_previous_version = (
            enum(UserRoleEnum).didnt_have("moderator"),
        )

        @convert_response_to_previous_version_for(UserResource)
        def change_moderator_to_regular(response: ResponseInfo):
            if response.body["role"] == "moderator":
                response.body["role"] = "regular"
    ```

We convert moderators to regulars in older versions because it is a safer choice for our users.


## Rename an endpoint
