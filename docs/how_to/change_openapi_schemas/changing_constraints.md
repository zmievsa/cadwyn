# Change field constraints or validators in openapi schemas

## Add or narrow constraints

Let's say that we previously allowed users to have a name of arbitrary length but now we want to limit it to 250 characters because we are worried that some users will be using inadequate lengths. You can't do this easily: if you simply add a `max_length` constraint to `User.name` -- the existing data in your database might become incompatible with this field in `UserResource`. So as long as incompatible data is there or can get there from some version -- you cannot add such a constraint to your responses. However, you **can** add it to your requests to prevent the creation of new user accounts with long names.

1. Change `max_length` of `data.latest.users.UserResource.name` to 250
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import VersionChange, schema
    from data.latest.users import UserCreateRequest


    class AddMaxLengthConstraintToUserNames(VersionChange):
        description = (
            "Add a max length of 250 to user names when creating new users "
            "to prevent overly large names from being used."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("name").didnt_have("max_length"),
        )
    ```

3. [Regenerate](../../concepts/code_generation.md) the versioned schemas

Note, however, that anyone using the old API versions will also not be able  will still be able to use arbitrary length names in older API versions. If you want to prevent that, then the correct approach would instead be the following:

0. Check whether any users have names longer than 250 characters. If there are few or no users that have such long names, then it may make sense to skip step 1. The other steps, however, cannot be skipped if you want to guarantee that your API gives no 500s at any point in the process.
1. Issue a 3-6 month warning to all users stating that you will make a breaking change affecting older versions. Mention that you will truncate old names that are longer than 250 characters and that users will no longer be able to create such long names even in old API versions.
2. After the deadline, add a `max_length` constraint to `data.latest.users.UserCreateRequest.name`
3. [Regenerate](../../concepts/code_generation.md) the versioned schemas
4. Release it to production
5. Truncate all names that are too long in the database (preferably using a migration and a separate release)
6. Remove the `max_length` constraint from `data.latest.users.UserCreateRequest.name`
7. Add the `max_length` constraint to `data.latest.users.User.name`
8. [Regenerate](../../concepts/code_generation.md) the versioned schemas

This process seems quite complex but it's not Cadwyn-specific: if you want to safely and nicely version for your users, you will have to follow such a process even if you don't use any versioning framework at all.

## Remove or expand constraints

Let's say that we previously only allowed users to have a name of length 50 but now we want to allow names of length 250 too. It does not make sense to add this to a new API version. Just add it into all API versions because it is not a breaking change.

The recommended approach:

1. Change `max_length` of `data.latest.users.User.name` to 250
2. [Regenerate](../../concepts/code_generation.md) the versioned schemas

However, sometimes it can be considered a breaking change if a large portion of your users use your system to verify their data and rely on your system to return status code `422` if this field is invalid. If that's the case, use the same approach as in [constraint addition](#add-or-narrow-constraints) but use `50` instead of `schema(UserCreateRequest).field("name").didnt_have("max_length")` for the old value.

## Add or remove validators

The same approach as above could be used to add or remove pydantic validator functions using [validator code generation](../../concepts/schema_migrations.md#add-a-validator-to-the-older-version).
