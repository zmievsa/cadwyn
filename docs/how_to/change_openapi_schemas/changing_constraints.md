# Change field constraints or validators in openapi schemas

## Add or narrow constraints

Let's say that we previously allowed users to have a name of arbitrary length but now we want to limit it to 250 characters because we are worried that some users will be using inadequate lengths. You can't do this easily: if you simply add a `max_length` constraint to `User.name` -- the existing data in your database might become incompatible with this field in `UserResource`. So as long as incompatible data is there or can get there from some version -- you cannot add such a constraint to your responses. However, you **can** add it to your requests to prevent the creation of new user accounts with long names.

1. Change `max_length` of `data.v2001_01_01.users.UserCreateRequest.name` to 250 by adding the following migration to `versions.v2001_01_01`. We do this instead of just adding the constraint to HEAD to make sure that old requests will get converted to HEAD successfully -- without facing the constraint:

    ```python
    from cadwyn import VersionChange, schema
    from users import UserCreateRequest


    class AddLengthConstraintToNameInLatest(VersionChange):
        description = (
            "Remove the max_length constraint from the HEAD version to support "
            "versions older than 2001_01_01 where it did not have the constraint."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("name").had(max_length=250),
        )
    ```

2. Then add this migration right under it into the same file:

    ```python
    class AddMaxLengthConstraintToUserNames(VersionChange):
        description = (
            "Add a max length of 250 to user names when creating new users "
            "to prevent overly large names from being used."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("name").didnt_have("max_length"),
        )
    ```

3. Add both of these migrations into the version bundle:

    ```python
    from cadwyn import Version, VersionBundle, HeadVersion
    from datetime import date
    from .v2001_01_01 import (
        AddLengthConstraintToNameInLatest,
        AddMaxLengthConstraintToUserNames,
    )

    version_bundle = VersionBundle(
        HeadVersion(AddLengthConstraintToNameInLatest),
        Version("2001-01-01", AddMaxLengthConstraintToUserNames),
        Version("2000-01-01"),
    )
    ```

So our HEAD version does not have this constraint, our latest does, and earlier versions do not.

* Requests from 2000 will be converted directly to HEAD and will not face this constraint because HEAD does not have it
* Requests from 2001 will first be validated by 2001 schemas with this constraint, and then will be converted to HEAD too

Note, however, that anyone using the old API versions will will still be able to use arbitrary length names in older API versions. If you want to prevent that, then the correct approach would instead be the following:

0. Check whether any users have names longer than 250 characters. If there are few or no users that have such long names, then it may make sense to skip step 1. The other steps, however, cannot be skipped if you want to guarantee that your API gives no 500s at any point in the process.
1. Issue a 3-6 month warning to all users stating that you will make a breaking change affecting older versions. Mention that you will truncate old names that are longer than 250 characters and that users will no longer be able to create such long names even in old API versions.
2. After the deadline, add a `max_length` constraint to `users.UserCreateRequest.name`
3. Release it to production
4. Truncate all names that are too long in the database (preferably using a migration and a separate release)
5. Remove the `max_length` constraint from `users.UserCreateRequest.name`
6. Add the `max_length` constraint to `users.BaseUser.name`

This process seems quite complex but it's not Cadwyn-specific: if you want to safely and nicely version for your users, you will have to follow such a process even if you don't use any versioning framework at all.

## Remove or expand constraints

Let's say that we previously only allowed users to have a name of length 50 but now we want to allow names of length 250 too. It does not make sense to add this to a new API version. Just add it into all API versions because it is not a breaking change.

You just need to change `max_length` of `users.BaseUser.name` to 250

However, sometimes it can be considered a breaking change if a large portion of your users use your system to verify their data and rely on your system to return status code `422` if this field is invalid. If that's the case, use the same approach as in [constraint addition](#add-or-narrow-constraints) but use `schema(UserCreateRequest).field("name").had(max_length=50)` instead.

## Add or remove validators

The same approach as above could be used to add or remove pydantic validator functions using [validator generation](../../concepts/schema_migrations.md#add-a-validator-to-the-older-version). Note that adding validators is the same as narrowing or adding constraints, which means that the same trick as above should be used.
