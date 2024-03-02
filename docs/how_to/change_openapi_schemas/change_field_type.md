# Change field type in schemas

## Incompatibly change the type

If your data had a type `int` and you want to change it to an `str` in a new version, then your data from the new version can easily break the responses of the old versions, thus making it [data versioning](../../concepts/beware_of_data_versioning.md), not API versioning -- as you are versioning the fundamental structures the user is operating on instead of just the API.

## Expand the type

Let's say that our clients could choose a `role` for our users. Originally, it was only possible to choose `admin` or `regular` but we would like to expand it to `moderator` which has all the powers of an admin except that moderators cannot assign other admins.

This is not a breaking change in terms of requests but it [**can be**](#why-enum-expansion-is-a-breaking-change-for-responses) a breaking change in terms of responses.

So if you do consider it a breaking change in terms of responses, you should do the following:

1. Add `moderator` value into `data.latest.users.UserRoleEnum`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import (
        VersionChange,
        enum,
        convert_response_to_previous_version_for,
        ResponseInfo,
    )
    from data.latest.users import UserRoleEnum, UserResource
    import datetime


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

3. [Regenerate](../../concepts/code_generation.md) the versioned schemas

We convert moderators to regulars in older versions because it is a safer choice for our users.

### Why enum expansion is a breaking change for responses

Let's that our schema includes a list that contains euros and/or dollars. Using our framework for unmarshalling JSON, we take the JSON string and try to convert it into the list of euros and/or dollars. If we suddenly see Georgian lari there -- our unmarshalling framework freaks out because the list is not what it expected, which makes adding an enum value a breaking change when you have a list of items.

To be more precise: If I, as a client, expect `Array<Euro | Dollar>`, then `Array<Euro>` would be a compatible response and `Array<Dollar>` would be a compatible response BUT `Array<Euro | Dollar | Lari>` would be an incompatible response.
That is the case because `Array<Euro | Dollar | Lari>` is a not a subtype of `Array<Euro | Dollar>` while `Array<Euro>` is.

In a sense, extending an enum that has `USD` with `USD | EUR` is equivalent to turning an `int` field into an `int | str` field, which is a breaking change. Hence extending an enum is often a breaking change and thus we might not need to solve this problem at all.

Additional resources:

* <https://github.com/OAI/OpenAPI-Specification/issues/1552>
* <https://users.rust-lang.org/t/solved-is-adding-an-enum-variant-a-breaking-change/26721/5>
* <https://github.com/graphql/graphql-js/issues/968>
* <https://medium.com/@jakob.fiegerl/java-jackson-enum-de-serialization-with-rest-backward-compatibility-9c3ec85ac13d>

In these sections, we'll be working with our user's response model: `data.latest.users.UserResource`. Note that the main theme here is "Will I be able to serialize this change to any of my versions?" as any change to responses can make them incompatible with the data in your database.

## Narrow the type

Let's say that previously users could specify their date of birth as a datetime instead of a date. We wish to rectify that. We can solve this with [internal body request schemas](../../concepts/version_changes.md#internal-request-body-representations).

0. Continue storing `date_of_birth` as a datetime in your database to avoid breaking any old behavior
1. Change the type of `date_of_birth` field to `datetime.date` in `data.latest.users.User`
2. Add a `data.unversioned.users.UserInternalCreateRequest` that we will use later to wrap migrated data instead of the latest request schema. This schema will allow us to keep time information from older versions without allowing users in new versions to provide it. This allows us to guarantee that old requests function in the same manner as before while new requests have the narrowed types.

    ```python
    from pydantic import Field
    from ..latest.users import UserCreateRequest
    import datetime


    class UserInternalCreateRequest(UserCreateRequest):
        time_of_birth: datetime.time = Field(default=datetime.time(0, 0, 0))
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
        convert_request_to_next_version_for,
        RequestInfo,
    )
    from data.latest.users import User, UserCreateRequest
    import datetime


    class ChangeDateOfBirthToDateInUser(VersionChange):
        description = (
            "Change 'User.date_of_birth' field type to date instead of "
            "a datetime because storing the exact time is unnecessary."
        )
        instructions_to_migrate_to_previous_version = (
            schema(User).field("date_of_birth").had(type=datetime.datetime),
        )

        @convert_request_to_next_version_for(UserCreateRequest)
        def add_time_field_to_request(request: RequestInfo):
            request.body["time_of_birth"] = request.body["date_of_birth"].time()
    ```

5. [Regenerate](../../concepts/code_generation.md) the versioned schemas
6. Within your business logic, create the datetime that you will put into the database by combining `date_of_birth` field and `time_of_birth` field

See how we did not need to use [convert_response_to_previous_version_for](../../concepts/version_changes.md#data-migrations)? We do not need to migrate anything because moving from `datetime` to `date` is easy: our database data already contains datetimes so pydantic will automatically narrow them to dates for responses if necessary. We also do not need to change anything about `date_of_birth` in the requests of older versions because our schema of the new version will automatically cast `datetime` to `date`.

This whole process was a bit complex so let us break it down a little:

1. `date_of_birth` field is a datetime in 2000 but a date in 2001.
2. We needed some way to keep the 2000 behavior without allowing users in 2001 to use it. Cadwyn always converts all requests to the latest version but the latest version doesn't have any time information in this case. The solution is to introduce an internal schema which will include everything from latest plus the time information.
3. When we receive a request from 2000, our migration fills up `time_of_birth` field. When we receive a request from 2001, the default value of `time(0, 0, 0)` is used.
4. Internally we keep working with datetimes. Now we build them from `date_of_birth` and `time_of_birth` fields.
5. When we return a response, we always return a datetime for `date_of_birth`. However, it is automatically converted to date by pydantic in 2001

Thus, we have kept old behavior, added new constrained behavior, and minimized the impact on our business logic as business logic simply doesn't know that `date_of_birth` in requests was ever a datetime and that `date_of_birth` in responses is ever a date. All of this information is hidden in our migration which will likely never change.
