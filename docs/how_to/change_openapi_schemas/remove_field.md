# Remove a field from openapi schemas

## From response

Let's say that our API has a mandatory `UserResource.date_of_birth` field. Let's also say that our API has previously exposed user's zodiac sign. Our analysts have decided that it does not make sense to store or send this information as it does not affect the functionality and can be inferred from date of birth.

1. Remove `zodiac_sign` field from `data.latest.users.UserResource`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import VersionChange, schema
    from data.latest.users import UserResource
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

3. [Regenerate](../../concepts/code_generation.md) the versioned schemas

Thanks to the version change above, your old schemas will now include `zodiac_sign` field but your new schemas will stay the same. Don't remove the zodiac business logic from your router because the old version will still need it. So you always return the zodiac sign but the schemas of the latest version will ignore it.

You can remove the logic for calculating and returning the zodiac sign after version `2000-01-01` gets deleted.

## From both request and response

### Optional field

Let's say that we had a nullable `middle_name` field but we decided that it does not make sense anymore and want to remove it now from both requests and responses. We can solve this with [internal body request schemas](../../concepts/version_changes.md#internal-request-body-representations).

1. Remove `middle_name` field from `data.latest.users.User`
2. Add a `data.unversioned.users.UserInternalCreateRequest` that we will use later to wrap migrated data instead of the latest request schema.

    ```python
    from pydantic import Field
    from ..latest.users import UserCreateRequest


    class UserInternalCreateRequest(UserCreateRequest):
        middle_name: str | None = Field(default=None)
    ```

3. Replace `UserCreateRequest` in your routes with `Annotated[UserInternalCreateRequest, InternalRepresentationOf[UserCreateRequest]]`:

    ```python
    from data.latest.users import UserCreateRequest, UserResource
    from cadwyn import InternalRepresentationOf
    from typing import Annotated


    @router.post("/users", response_model=UserResource)
    async def create_user(
        user: Annotated[
            UserInternalCreateRequest, InternalRepresentationOf[UserCreateRequest]
        ]
    ):
        ...
    ```

4. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import (
        VersionChange,
        schema,
        RequestInfo,
    )
    from data.latest.users import User


    class RemoveMiddleNameFromUser(VersionChange):
        description = "Remove 'User.middle_name' field."
        instructions_to_migrate_to_previous_version = (
            schema(User)
            .field("middle_name")
            .existed_as(
                type=str | None, description="User's Middle Name", default=None
            ),
        )
    ```

5. [Regenerate](../../concepts/code_generation.md) the versioned schemas

Note that in order for this to work, you would still have to store `middle_name` in your database and return it with your responses.

Thus we have added a new API version and our business logic hasn't even noticed it.

### Required field

There are two main cases with required fields:

1. Remove a required field with a simple fake default (created_at)
2. Remove a required field with an impossible default (tax id)

The first one is simple to solve: just use the approach [above](#remove-optional-fields-from-schemas) but use a `default_factory=datetime.datetime.now` instead of `default=None` within `UserInternalCreateRequest`.

Now what about case 2?

Let's say that you have company resources in your system. Let's also say that each company has a `tax_id` and now you would like to remove the `tax_id` field or make it optional. If `tax_id` was required in your responses, you can't really do this with traditional API versioning because you cannot come up with a sane non-null default for `tax_id`. It is a case of [data versioning](../../concepts/beware_of_data_versioning.md) where you try to make an API version that is inconsistent with other API versions in terms of its data. You deal with this using one of the following approaches:

0. Talk to your users. In any API versioning problem, talking to your users is the best first step. See whether this is actually a breaking change for them. Maybe only a small subset of your users is using this field and you can migrate this subset manually without much investment, which will allow you to make the breaking changes without breaking anyone's API. Though this approach becomes impossible to use once you get a lot of clients.
1. Issue a warning to your users that `tax_id` is going to become optional in all API versions in `N` months and then make it so. This will allow you to avoid data versioning and your users will have a grace period to fix their issues. Then you can simply follow the [approach above](#remove-optional-fields-from-schemas).
2. Release a `V2` version of your API which users will have to migrate their **data** to. This is a drastic approach and you should only reserve it for extreme cases but it is a **correct** way to represent data versioning.
3. Disallow the new version (2001-01-01) to be used alongside older versions and disallow users to migrate to older versions after they have migrated to 2001-01-01. Then you can simply follow the [approach above](#remove-optional-fields-from-schemas). This is a dirty hack and an inconvenience to your users but it solves the problem too, albeit I would never recommend to use this solition.
