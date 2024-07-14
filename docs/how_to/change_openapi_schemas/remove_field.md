# Remove a field from openapi schemas

## From response

Let's say that our API has a mandatory `UserResource.date_of_birth` field. Let's also say that our API has previously exposed user's zodiac sign. Our analysts have decided that it does not make sense to store or send this information as it does not affect the functionality and can be inferred from date of birth.

1. Remove `zodiac_sign` field from `users.UserResource`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn import VersionChange, schema
    from users import UserResource
    from pydantic import Field


    class RemoveZodiacSignFromUser(VersionChange):
        description = (
            "Remove 'zodiac_sign' field from UserResource because "
            "it can be inferred from user's date of birth and because "
            "only a small number of users has utilized it."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserResource)
            .field("zodiac_sign")
            .existed_as(type=str, info=Field(description="User's magical sign")),
        )
    ```

3. Add this migration into the version bundle:

    ```python
    # versions/__init__.py

    from cadwyn import Version, VersionBundle, HeadVersion
    from datetime import date
    from .v2001_01_01 import RemoveZodiacSignFromUser

    version_bundle = VersionBundle(
        HeadVersion(),
        Version("2001-01-01", RemoveZodiacSignFromUser),
        Version("2000-01-01"),
    )
    ```

Thanks to the version change above, your old schemas will now include `zodiac_sign` field but your new schemas will stay the same. Don't remove the zodiac business logic from your router because the old version will still need it. So you always return the zodiac sign but the schemas of the latest version will ignore it.

You can remove the logic for calculating and returning the zodiac sign after version `2000-01-01` gets deleted.

## From both request and response

### Optional field

Let's say that we had a nullable `middle_name` field but we decided that it does not make sense anymore and want to remove it now from both requests and responses. This means that a user from an old version will still be able to pass it while the user from a new version will not. We can solve this by having this field in our HEAD, removing it from our latest version but keeping it in all older versions:

0. Keep storing `middle_name` in your database in order to support old versions
1. Add the following migration to `versions.v2001_01_01` to remove `middle_name` from the latest version:

    ```python
    from cadwyn import VersionChange, schema
    from users import BaseUser


    class RemoveMiddleNameFromLatestVersion(VersionChange):
        description = (
            "Remove 'User.middle_name' from latest but keep it in HEAD "
            "to support versions before 2001-01-01."
        )
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser).field("middle_name").didnt_exist,
        )
    ```

2. Add the following migration to `versions.v2001_01_01` to leave support for `middle_name` in the older versions:

    ```python
    from cadwyn import VersionChange, schema
    from users import BaseUser


    class RemoveMiddleNameFromUser(VersionChange):
        description = "Remove 'User.middle_name' field"
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser)
            .field("middle_name")
            .existed_as(
                type=str | None, description="User's Middle Name", default=None
            ),
        )
    ```

3. Add these migrations into the version bundle:

    ```python
    # versions/__init__.py

    from cadwyn import Version, VersionBundle, HeadVersion
    from datetime import date
    from .v2001_01_01 import RemoveZodiacSignFromUser

    version_bundle = VersionBundle(
        HeadVersion(RemoveMiddleNameFromLatestVersion),
        Version("2001-01-01", RemoveMiddleNameFromUser),
        Version("2000-01-01"),
    )
    ```

We added a new version with a breaking change but neither the HEAD schema that we use in business logic, neither has the business logic itself have changed one bit.

### Required field

There are two main cases with required fields:

1. Remove a required field with a simple fake default (created_at)
2. Remove a required field with an impossible default (tax id)

The first one is simple to solve: just use the approach [above](#optional-field) but use a `default_factory=datetime.datetime.now` instead of `default=None`.

Now what about case 2?

Let's say that you have company resources in your system. Let's also say that each company has a `tax_id` and now you would like to remove the `tax_id` field or make it optional. If `tax_id` was required in your responses, you can't really do this with traditional API versioning because you cannot come up with a sane non-null default for `tax_id`. It is a case of [data versioning](../../concepts/beware_of_data_versioning.md) where you try to make an API version that is inconsistent with other API versions in terms of its data. You deal with this using one of the following approaches:

0. Talk to your users. In any API versioning problem, talking to your users is the best first step. See whether this is actually a breaking change for them. Maybe only a small subset of your users is using this field and you can migrate this subset manually without much investment, which will allow you to make the breaking changes without breaking anyone's API. Though this approach becomes impossible to use once you get a lot of clients.
1. Issue a warning to your users that `tax_id` is going to become optional in all API versions in `N` months and then make it so. This will allow you to avoid data versioning and your users will have a grace period to fix their issues. Then you can simply follow the [approach above](#optional-field).
2. Release a `V2` version of your API which users will have to migrate their **data** to. This is a drastic approach and you should only reserve it for extreme cases but it is a **correct** way to represent data versioning.
3. Disallow the new version (2001-01-01) to be used alongside older versions and disallow users to migrate to older versions after they have migrated to 2001-01-01. Then you can simply follow the [approach above](#optional-field). This is a dirty hack and an inconvenience to your users but it solves the problem too, albeit I would never recommend to use this solition.
