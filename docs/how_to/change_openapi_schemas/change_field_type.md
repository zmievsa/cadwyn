# Change field type in schemas

## Incompatibly change the type

If your data had a type `int` and you want to change it to an `str` in a new version, then your data from the new version can easily break the responses of the old versions, thus making it [data versioning](../../concepts/beware_of_data_versioning.md), not API versioning -- as you are versioning the fundamental structures the user is operating on instead of just the API.

## Expand the type

Let's say that our clients could choose a `role` for our users. Originally, it was only possible to choose `admin` or `regular` but we would like to expand it to `moderator` which has all the powers of an admin except that moderators cannot assign other admins.

This is not a breaking change in terms of requests but it [**can be**](#why-enum-expansion-is-a-breaking-change-for-responses) a breaking change in terms of responses.

So if you do consider it a breaking change in terms of responses, you should do the following:

1. Add `moderator` value into `users.BaseUserRoleEnum`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn import (
        VersionChange,
        enum,
        convert_response_to_previous_version_for,
        ResponseInfo,
    )
    from users import UserRoleEnum, UserResource
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

In these sections, we'll be working with our user's response model: `users.UserResource`. Note that the main theme here is "Will I be able to serialize this change to any of my versions?" as any change to responses can make them incompatible with the data in your database.

## Narrow the type

Let's say that previously users could specify their date of birth as a datetime instead of a date. We wish to rectify that. We can solve this by making it a datetime in HEAD version, converting it to date in latest version, and then making it a datetime again in the old versions. So whenever we receive a request in an old version, it will get converted to HEAD version where it is a datetime. And whenever we receive a request in latest version, it will also be converted to HEAD where date will simply be casted to datetime with time = 00:00:00.

0. Continue storing `date_of_birth` as a datetime in your database to avoid breaking any old behavior
1. Add the following migration to `versions.v2001_01_01` which will turn `date_of_birth` into a date in 2001_01_01. Note how we use the validator for making sure that `date_of_birth` is converted to date in the latest version. It is only necessary in Pydantic 2 because it has no implicit casting from datetime to date. Note also how we use strings for types: this is not always necessary; it just allows you to control specifically how Cadwyn is going to render your types. Most of the time you won't need to use strings for types.

    ```python
    from cadwyn import VersionChange, schema
    from pydantic import validator
    from users import BaseUser
    import datetime


    @field_validator("date_of_birth", mode="before")
    def convert_date_of_birth_to_date(cls, v: datetime.date | datetime.datetime):
        if isinstance(v, datetime.datetime):
            return v.date()
        return v


    class ChangeDateOfBirthToDateInUserInLatest(VersionChange):
        description = (
            "Change 'BaseUser.date_of_birth' field type to datetime in HEAD "
            "to support versions and data before 2001-01-01. "
        )
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser).field("date_of_birth").had(type=datetime.date),
            # This step is only necessary in Pydantic 2 because datetime won't be converted
            # to date automatically.
            schema(BaseUser).validator(convert_date_of_birth_to_date).existed,
        )
    ```

2. Add the following version change to `versions.v2001_01_01` (right under the version change above) which will make sure that `date_of_birth` is a datetime in 2000_01_01:

    ```python
    class ChangeDateOfBirthToDateInUser(VersionChange):
        description = (
            "Change 'User.date_of_birth' field type to date instead of "
            "a datetime because storing the exact time is unnecessary."
        )
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser).field("date_of_birth").had(type=datetime.datetime),
            schema(BaseUser).validator(convert_date_of_birth_to_date).didnt_exist,
        )
    ```

3. Add both migrations into our VersionBundle:

    ```python
    from cadwyn import Version, VersionBundle, HeadVersion
    from datetime import date
    from .v2001_01_01 import MakePhoneNonNullableInLatest, AddPhoneToUser


    version_bundle = VersionBundle(
        HeadVersion(ChangeDateOfBirthToDateInUserInLatest),
        Version("2001-01-01", ChangeDateOfBirthToDateInUser),
        Version("2000-01-01"),
    )
    ```

This whole process was a bit complex so let us break it down a little:

1. `date_of_birth` field is a datetime in HEAD, a date in 2001, and a datetime again in 2000.
2. We needed some way to keep the 2000 behavior without allowing users in 2001 to use it. Cadwyn always converts all requests to the HEAD version so:
    * When we receive user creation requests from 2001, we convert them directly to HEAD, and pydantic casts date to datetime without any issue
    * When we receive user get requests from 2001, we convert them directly from HEAD to latest, and our validator casts datetime to date (note that pydantic 1 would be able to do it even without a validator)
    * When we receive user creation requests from 2000, we convert them directly to HEAD -- they have the same type for `date_of_birth` so it is easy to Cadwyn
    * When we receive user get requests from 2000, we convert them directly from HEAD to 2000 -- they have the same type for `date_of_birth` so it is easy to Cadwyn

All of these interactions are done internally by Cadwyn. As you see, the process is more than straightforward: requests are converted to HEAD, and responses are converted from HEAD.

Thus, we have kept old behavior, added new constrained behavior, and minimized the impact on our business logic as business logic simply doesn't know that `date_of_birth` in requests is ever a date and that `date_of_birth` in responses is ever a date. All of this information is hidden in our migration.

A very important point here is that unlike schemas, routes, and business logic -- the migrations we wrote will likely never need to change because they describe the fundamental differences between the API versions, and these differences cannot be changed in the future because that would defeat the purpose of API versioning. This makes migrations effectively immutable and consequently very cheap to support.
