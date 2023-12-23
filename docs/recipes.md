# Recipes

This section serves as a guide to help you create new versions effectively and maintain your old versions with ease. It consists of a collection of scenarios for different types of breaking changes.

To follow this guide, choose a type of an entity that you'd like to alter (an endpoint, a schema, etc), find the respective level two subheading, and follow its instructions. Repeat for every breaking change you would like to do.

The guide will assume the following directory structure:
<!--- Find a better name for "data" dir. "Schemas" doesn't work because enums can also be there. "Versioned" doesn't work because unversioned stuff can also be there as long as it's not in latest.-->
```tree
├── routes.py
├── data
│   ├── __init__.py
│   ├── unversioned
│   │   ├── __init__.py
│   │   └── users.py
│   └── latest
│       ├── __init__.py
│       └── users.py
└── versions
    ├── __init__.py
    └── v2001_01_01.py
```

Schemas, enums, and any other versioned data are inside the `data.latest` package, version changes are inside the `versions.vXXXX_XX_XX` modules, and version bundle is inside the `versions.__init__` module. It includes all versions with all version changes -- including the ones you add in the recipes.

You can assume that we already have a version **2000-01-01** and we are making a new version **2001-01-01** with the changes from our scenarios.

Versioning is a complex topic with more pitfalls than you'd expect so please: **do not try to skip this guide**. Otherwise, your code will quickly get unmaintainable. Please also note that any of these scenarios can be combined in any way even within a single version change, though it's recommended to keep the version changes atomic as described in [methodology](#methodology) section.

## Methodology

Cadwyn implements a methodology that is based on the following set of principles:

* Each version is made up of "version changes" or "compatibility gates" which describe **independent atomic** differences between it and previous version
* We make a new version if an only if we have breaking changes
* Versions must have little to no effect on the business logic
* Versions **must always** be compatible in terms of data
* Creating new versions is avoided at all costs
* Any backwards compatible features must be backported to all compatible versions

These rules give us an ability to have a large number of self-documenting versions while encapsulating their complexity in small version change classes, providing a consistent and stable experience to our users.

So if we see that we need to make a breaking change, our general approach is to:

1. Make the breaking change in your schemas, routes, or business logic
2. Write a version change class (and sometimes [a little extra](#side-effects)) that describes the difference between the new version and the old version

## A note on data versioning

Oftentimes you will want to introduce a breaking change where one of the following is true:

* Old data cannot be automatically converted to the structure of the new response
* New response cannot be automatically migrated to an older response
* Old request cannot be automatically converted to the latest or internal request

This means that you are not versioning your API, you are versioning your **data**. This is not and cannot be solved by an API versioning framework. It also makes it incredibly hard to version as you now cannot guarantee compatibility between versions. Avoid this at all costs -- all your API versions must be compatible between each other.

## Schemas (openapi data type)

### Requests and Responses

In this section, we will cover situations where both requests and respones are affected. We'll be working with our user's response and request models: `UserResource` and `UserCreateRequest`. Both of them are located in `data.latest.users` and both of them share a parent class: `User` that contains the fields shared by both requests and responses.

#### Schema field renaming

Let's say that we had a "summary" field before but now we want to rename it to "bio".

1. Rename `summary` field to `bio` in `data.latest.users.User`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import (
        VersionChange,
        schema,
        convert_response_to_previous_version_for,
        convert_request_to_next_version_for,
        ResponseInfo,
        RequestInfo,
    )
    from data.latest.users import User, UserCreateRequest, UserResource


    class RenameSummaryIntoBioInUser(VersionChange):
        description = (
            "Rename 'summary' field into 'bio' to keep up with industry standards"
        )
        instructions_to_migrate_to_previous_version = (
            schema(User).field("bio").had(name="summary"),
        )

        @convert_request_to_next_version_for(UserCreateRequest)
        def rename_bio_to_summary(request: RequestInfo):
            request.body["summary"] = request.body.pop("bio")

        @convert_response_to_previous_version_for(UserResource)
        def rename_bio_to_summary(response: ResponseInfo):
            response.body["bio"] = response.body.pop("summary")
    ```

3. [Regenerate](./reference.md#code-generation) the versioned schemas

#### Addition or Narrowing of constraints

Let's say that we previously allowed users to have a name of arbitrary length but now we want to limit it to 250 characters because we are worried that some users will be using inadequate lengths. You can't do this easily: if you simply add a `max_length` constraint to `User.name` -- the existing data in your database might become incompatible with this field in `UserResource`. So as long as incompatible data is there or can get there from some version -- you cannot add such a constraint to your responses. However, you **can** add it to your requests to prevent the creation of new user accounts with long names.

1. Change `max_length` of `data.latest.users.UserResource.name` to 250
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import VersionChange, schema
    from data.latest.users import UserCreateRequest

    # Note that in pydantic v2 this would be `from pydantic_core import PydanticUndefined`
    from pydantic.fields import Undefined


    class AddMaxLengthConstraintToUserNames(VersionChange):
        description = (
            "Add a max length of 250 to user names when creating new users "
            "to prevent overly large names from being used."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("name").had(max_length=Undefined),
        )
    ```

3. [Regenerate](./reference.md#code-generation) the versioned schemas

Note, however, that users will still be able to use arbitrary length names in older API versions. If you want to prevent that, then the correct approach would instead be the following:

0. Check whether any users have names longer than 250 characters. If there are few or no users that have such long names, then it may make sense to skip step 1. The other steps, however, cannot be skipped if you want to guarantee that your API gives no 500s at any point in the process.
1. Issue a 3-6 month warning to all users stating that you will make a breaking change affecting older versions. Mention that you will truncate old names that are longer than 250 characters and that users will no longer be able to create such long names even in old API versions.
2. After the deadline, add a `max_length` constraint to `data.latest.users.UserCreateRequest.name`
3. [Regenerate](./reference.md#code-generation) the versioned schemas
4. Release it to production
5. Truncate all names that are too long in the database (preferably using a migration and a separate release)
6. Remove the `max_length` constraint from `data.latest.users.UserCreateRequest.name`
7. Add the `max_length` constraint to `data.latest.users.User.name`
8. [Regenerate](./reference.md#code-generation) the versioned schemas

This process seems quite complex but it's not Cadwyn-specific: if you want to safely and nicely version for your users, you will have to follow such a process even if you don't use any versioning framework at all.

#### Removal or Expansion of constraints

Let's say that we previously only allowed users to have a name of length 50 but now we want to allow names of length 250 too. It does not make sense to add this to a new API version. Just add it into all API versions because it is not a breaking change.

The recommended approach:

1. Change `max_length` of `data.latest.users.User.name` to 250
2. [Regenerate](./reference.md#code-generation) the versioned schemas

However, sometimes it can be considered a breaking change if a large portion of your users use your system to verify their data and rely on your system to return status code `422` if this field is invalid. If that's the case, use the same approach as in [constraint addition](#addition-or-narrowing-of-constraints) but use `50` instead of `pydantic.fields.Undefined` for the old value.

#### Schema field removal

##### Schema optional field removal

Let's say that we had a nullable `middle_name` field but we decided that it does not make sense anymore and want to remove it now from both requests and responses. We can solve this with [internal body request schemas](./reference.md#internal-request-schemas).

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
            InternalUserCreateRequest, InternalRepresentationOf[UserCreateRequest]
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

5. [Regenerate](./reference.md#code-generation) the versioned schemas

Note that in order for this to work, you would still have to store `middle_name` in your database and return it with your responses.

Thus we have added a new API version and our business logic hasn't even noticed it.

##### Schema required field removal

There are two main cases with required fields:

1. Remove a required field with a simple fake default (created_at)
2. Remove a required field with an impossible default (tax id)

The first one is simple to solve: just use the approach [above](#schema-optional-field-removal) but use a `default_factory=datetime.datetime.now` instead of `default=None` within `UserInternalCreateRequest`.

Now what about case 2?

Let's say that you have company resources in your system. Let's also say that each company has a `tax_id` and now you would like to remove the `tax_id` field or make it optional. If `tax_id` was required in your responses, you can't really do this with traditional API versioning because you cannot come up with a sane non-null default for `tax_id`. It is a case of [data versioning](#a-note-on-data-versioning) where you try to make an API version that is inconsistent with other API versions in terms of its data. You deal with this using one of the following approaches:

1. Issue a warning to your users that `tax_id` is going to become optional in all API versions in `N` months and then make it so. This will allow you to avoid data versioning and your users will have a grace period to fix their issues. Then you can simply follow the [approach above](#schema-optional-field-removal).
2. Release a `V2` version of your API which users will have to migrate their **data** to. This is a drastic approach and you should only reserve it for extreme cases but it is a **correct** way to represent data versioning.
3. Disallow the new version (2001-01-01) to be used alongside older versions and disallow users to migrate to older versions after they have migrated to 2001-01-01. Then you can simply follow the [approach above](#schema-optional-field-removal). This is a dirty hack and an inconvenience to your users but it solves the problem too.

#### Schema field addition

#### Schema optional field addition

Let's say we want our users to be able to specify a middle name but it is nullable. It is not a breaking change so no new version is necessary whether it is requests or responses.

The recommended approach:

1. Add a nullable `middle_name` field into `data.latest.users.User`
2. [Regenerate](./reference.md#code-generation) the versioned schemas

#### Schema required field addition

##### With compatible default value in older versions

Let's say that our users had a field `country` that defaulted to `USA` but our product is now used well beyond United States so we want to make this field required in the `latest` version.

1. Remove `default="US"` from `data.latest.users.UserCreateRequest`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import (
        VersionChange,
        schema,
        convert_request_to_next_version_for,
    )
    from data.latest.users import UserCreateRequest, UserResource


    class MakeUserCountryRequired(VersionChange):
        description = 'Make user country required instead of the "USA" default'
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("country").had(default="USA"),
        )

        @convert_request_to_next_version_for(UserCreateRequest)
        def add_time_field_to_request(request: RequestInfo):
            request.body["country"] = request.body.get("country", "USA")
    ```

3. [Regenerate](./reference.md#code-generation) the versioned schemas

That's it! Our old schemas will now contain a default but in `latest` country will be required. You might notice a weirdness: if we set a default in the old version, why would we also write a migration? That's because of a sad implementation detail of pydantic that [prevents us](./reference.md#defaults-warning) from using defaults from old versions.

##### With incompatible default value in older versions

Let's say that we want to add a required field `phone` to our users. However, older versions did not have such a field at all. This means that the field is going to be nullable in the old versions but required in the latest version. This also means that older versions contain a wider type (`str | None`) than the latest version (`str`). So when we try to migrate request bodies from the older versions to latest -- we might receive a `ValidationError` because `None` is not an acceptable value for `phone` field in the new version. Whenever we have a problem like this, when older version contains more data or a wider type set of data,  we use [internal body request schemas](./reference.md#internal-request-schemas).

1. Add `phone` field of type `str` to `data.latest.users.UserCreateRequest`
2. Add `phone` field of type `str | None` with a `default=None` to `data.latest.users.UserResource` because all users created with older versions of our API won't have phone numbers.
3. Add a `data.unversioned.users.UserInternalCreateRequest` that we will use later to wrap migrated data instead of the latest request schema. It will allow us to pass a `None` to `phone` from older versions while also guaranteeing that it is non-nullable in our latest version.

    ```python
    from pydantic import Field
    from ..latest.users import UserCreateRequest


    class UserInternalCreateRequest(UserCreateRequest):
        phone: str | None = Field(default=None)
    ```

4. Replace `UserCreateRequest` in your routes with `Annotated[UserInternalCreateRequest, InternalRepresentationOf[UserCreateRequest]]`:

    ```python
    from data.latest.users import UserCreateRequest, UserResource
    from cadwyn import InternalRepresentationOf
    from typing import Annotated


    @router.post("/users", response_model=UserResource)
    async def create_user(
        user: Annotated[
            InternalUserCreateRequest, InternalRepresentationOf[UserCreateRequest]
        ]
    ):
        ...
    ```

5. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn.structure import (
        VersionChange,
        schema,
    )
    from data.latest.users import UserCreateRequest, UserResource


    class AddPhoneToUser(VersionChange):
        description = (
            "Add a required phone field to User to allow us to do 2fa and to "
            "make it possible to verify new user accounts using an sms."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest)
            .field("phone")
            .had(
                type=str | None,
                default=None,
            ),
        )
    ```

6. [Regenerate](./reference.md#code-generation) the versioned schemas

See how we didn't remove the `phone` field from old versions? Instead, we allowed a nullable `phone` field to be passed into both old `UserResource` and old `UserCreateRequest`. This gives our users new functionality without needing to update their API version! It is one of the best parts of Cadwyn's approach: our users can get years worth of updates without switching their API version and without their integration getting broken.

#### Schema field type change or narrowing

##### Compatible narrowing

Let's say that previously users could specify their date of birth as a datetime instead of a date. We wish to rectify that. We can solve this with [internal body request schemas](./reference.md#internal-request-schemas).

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
            InternalUserCreateRequest, InternalRepresentationOf[UserCreateRequest]
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

5. [Regenerate](./reference.md#code-generation) the versioned schemas
6. Within your business logic, create the datetime that you will put into the database by combining `date_of_birth` field and `time_of_birth` field

See how we did not need to use [convert_response_to_previous_version_for](./reference.md#data-migrations)? We do not need to migrate anything because moving from `datetime` to `date` is easy: our database data already contains datetimes so pydantic will automatically narrow them to dates for responses if necessary. We also do not need to change anything about `date_of_birth` in the requests of older versions because our schema of the new version will automatically cast `datetime` to `date`.

This whole process was a bit complex so let us break it down a little:

1. `date_of_birth` field is a datetime in 2000 but a date in 2001.
2. We needed some way to keep the 2000 behavior without allowing users in 2001 to use it. Cadwyn always converts all requests to the latest version but the latest version doesn't have any time information in this case. The solution is to introduce an internal schema which will include everything from latest plus the time information.
3. When we receive a request from 2000, our migration fills up `time_of_birth` field. When we receive a request from 2001, the default value of `time(0, 0, 0)` is used.
4. Internally we keep working with datetimes. Now we build them from `date_of_birth` and `time_of_birth` fields.
5. When we return a response, we always return a datetime for `date_of_birth`. However, it is automatically converted to date by pydantic in 2001

Thus, we have kept old behavior, added new constrained behavior, and minimized the impact on our business logic as business logic simply doesn't know that `date_of_birth` in requests was ever a datetime and that `date_of_birth` in responses is ever a date. All of this information is hidden in our migration which will likely never change.

##### Incompatible narrowing/change

[It is data versioning](#a-note-on-data-versioning).

#### Schema field type expansion (including enum expansion)

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

3. [Regenerate](./reference.md#code-generation) the versioned schemas

We convert moderators to regulars in older versions because it is a safer choice for our users.

##### Why enum expansion is a breaking change for responses

Let's that our schema includes a list that contains euros and/or dollars. Using our framework for unmarshalling JSON, we take the JSON string and try to convert it into the list of euros and/or dollars. If we suddenly see Georgian lari there -- our unmarshalling framework freaks out because the list is not what it expected, which makes adding an enum value a breaking change when you have a list of items.

To be more precise: If I, as a client, expect `Array<Euro | Dollar>`, then `Array<Euro>` would be a compatible response and `Array<Dollar>` would be a compatible response BUT `Array<Euro | Dollar | Lari>` would be an incompatible response.
That is the case because `Array<Euro | Dollar | Lari>` is a not a subtype of `Array<Euro | Dollar>` while `Array<Euro>` is.

In a sense, extending an enum that has `USD` with `USD | EUR` is equivalent to turning an `int` field into an `int | str` field, which is a breaking change. Hence extending an enum is often a breaking change and thus we might not need to solve this problem at all.

Additional resources:

* <https://github.com/OAI/OpenAPI-Specification/issues/1552>
* <https://users.rust-lang.org/t/solved-is-adding-an-enum-variant-a-breaking-change/26721/5>
* <https://github.com/graphql/graphql-js/issues/968>
* <https://medium.com/@jakob.fiegerl/java-jackson-enum-de-serialization-with-rest-backward-compatibility-9c3ec85ac13d>

### Responses

In these sections, we'll be working with our user's response model: `data.latest.users.UserResource`. Note that the main theme here is "Will I be able to serialize this change to any of my versions?" as any change to responses can make them incompatible with the data in your database.

#### Response schema field addition

Let's say that we decided to expose the creation date of user's account with a `created_at` field in our API. This is **not** a breaking change so a new version is completely unnecessary. However, if you believe that you absolutely have to make a new version, refer to the [... section](#what-do-we-put-here) which lists the actions you would need to perform in such a situation.

The recommended approach:

1. Add `created_at` field into `data.latest.users.UserResource`
2. [Regenerate](./reference.md#code-generation) the versioned schemas

Now you have everything you need at your disposal: field `created_at` is available in all versions and your users do not even need to do any extra actions. Just make sure that the data for it is available in all versions too. If it's not: make the field optional.

#### Response schema field removal

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

3. [Regenerate](./reference.md#code-generation) the versioned schemas

Thanks to the version change above, your old schemas will now include `zodiac_sign` field but your new schemas will stay the same. Don't remove the zodiac business logic from your router because the old version will still need it. So you always return the zodiac sign but the schemas of the latest version will ignore it.

You can remove the logic for calculating and returning the zodiac sign after version `2000-01-01` gets deleted.

## Path changes

### Path addition

It is not a breaking change so it's recommended to simply add it to all versions. If you believe that you still need it, you can use the [following migration](./reference.md#defining-endpoints-that-didnt-exist-in-old-versions).

### Path deletion

See [reference](./reference.md#defining-endpoints-that-didnt-exist-in-new-versions)

## Behavior

First, ask yourself: are you sure there really needs to be a behavioral change? Are you sure it is not possible to keep the same logic for both versions? Or at least make the behavior depend on the received data? Behavioral changes (or **side effects**) are the least maintainable part of almost any versioning approach. They produce the largest footprint on your code so if you are not careful -- your logic will be littered with version checks.

But if you are certain that you need to make a breaking behavioral change, Cadwyn has all the tools to minimize its impact as much as possible.

The following sections are all done using a [version change with side effects](./reference.md#version-changes-with-side-effects).

### Calling endpoint causes unexpected data modifications

### Calling endpoint doesn't cause expected data modifications

### Calling endpoint doesn't cause expected additional actions (e.g. Webhooks)

### Errors

#### Error status or message change

#### New error introduced

#### Old error removed
