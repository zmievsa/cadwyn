# Add a field to openapi schemas

## To response schema

Let's say that we decided to expose the creation date of user's account with a `created_at` field in our API. This is **not** a breaking change so a new version is completely unnecessary. However, if you believe that you absolutely have to make a new version, then you can simply follow the recommended approach below but add a version change with [field didnt exist instruction](../../concepts/schema_migrations.md#remove-a-field-from-the-older-version).

You just need to add `created_at` field into `users.UserResource`.

Now you have everything you need at your disposal: field `created_at` is available in all versions and your users do not even need to do any extra actions. Just make sure that the data for it is available in all versions too. If it's not: make the field optional.

## To both request and response schemas

### Field is optional

Let's say we want our users to be able to specify a middle name but it is nullable. It is not a breaking change so no new version is necessary whether it is requests or responses.

You just need to add a nullable `middle_name` field into `users.BaseUser`

### Field is required

#### With compatible default value in older versions

Let's say that our users had a field `country` that defaulted to `USA` but our product is now used well beyond United States so we want to make this field required in the HEAD version.

1. Remove `default="US"` from `users.UserCreateRequest`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn import (
        VersionChange,
        schema,
        convert_request_to_next_version_for,
    )
    from users import UserCreateRequest, UserResource


    class MakeUserCountryRequired(VersionChange):
        description = 'Make user country required instead of the "USA" default'
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("country").had(default="USA"),
        )

        @convert_request_to_next_version_for(UserCreateRequest)
        def add_default_value_to_country_field_in_request(request: RequestInfo):
            request.body["country"] = request.body.get("country", "USA")
    ```

3. Add this migration into the version bundle:

    ```python
    from cadwyn import Version, VersionBundle, HeadVersion
    from datetime import date
    from .v2001_01_01 import MakeUserCountryRequired

    version_bundle = VersionBundle(
        HeadVersion(),
        Version("2001-01-01", MakeUserCountryRequired),
        Version("2000-01-01"),
    )
    ```


That's it! Our old schemas will now contain a default but in HEAD country will be required. You might notice a weirdness: if we set a default in the old version, why would we also write a migration? That's because of a sad implementation detail of pydantic that [prevents us](../../concepts/schema_migrations.md#change-a-field-in-the-older-version) from using defaults from old versions.

#### With incompatible default value in older versions

Let's say that we want to add a required field `phone` to our users. However, older versions did not have such a field at all. This means that the field is going to be nullable (or nonexistent) in the old versions but required in the HEAD version. This also means that older versions contain a wider type (`str | None`) than the HEAD version (`str`). So when we try to migrate request bodies from the older versions to HEAD -- we might receive a `ValidationError` because `None` is not an acceptable value for `phone` field in the new version. Whenever we have a problem like this, when older version contains more data or a wider type set of data,  we can simply define a wider type in our HEAD version and then narrow it in latest.

So we will make `phone` nullable in HEAD, then make it required in `latest`, and then make it nullable again in older versions, thus making it possible to convert all of our requests to HEAD.

1. Add `phone` field of type `str | None` to `users.BaseUser`
2. Add `phone` field of type `str | None` with a `default=None` to `users.UserResource` because all users created with older versions of our API won't have phone numbers.
3. Add the following migration to `versions.v2001_01_01` which will make sure that `phone` is not nullable in 2001_01_01:

    ```python
    from cadwyn import VersionChange, schema
    from users import UserCreateRequest


    class MakePhoneNonNullableInLatest(VersionChange):
        description = (
            "Make sure the phone is nullable in the HEAD version to support "
            "versions older than 2001_01_01 where it became non-nullable"
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest).field("phone").had(type=str),
            schema(UserCreateRequest).field("phone").didnt_have("default"),
        )
    ```

4. Add the following version change to `versions.v2001_01_01` (right under the version change above) which will make sure that `phone` is nullable in 2000_01_01:

    ```python
    class AddPhoneToUser(VersionChange):
        description = (
            "Add a required phone field to User to allow us to do 2fa and to "
            "make it possible to verify new user accounts using an sms."
        )
        instructions_to_migrate_to_previous_version = (
            schema(UserCreateRequest)
            .field("phone")
            .had(type=str | None, default=None),
        )
    ```

5. Add both migrations into our VersionBundle:

    ```python
    from cadwyn import Version, VersionBundle, HeadVersion
    from datetime import date
    from .v2001_01_01 import MakePhoneNonNullableInLatest, AddPhoneToUser


    version_bundle = VersionBundle(
        HeadVersion(MakePhoneNonNullableInLatest),
        Version("2001-01-01", AddPhoneToUser),
        Version("2000-01-01"),
    )
    ```


See how we didn't remove the `phone` field from old versions? Instead, we allowed a nullable `phone` field to be passed into both old `UserResource` and old `UserCreateRequest`. This gives our users new functionality without needing to update their API version! It is one of the best parts of Cadwyn's approach: our users can get years worth of updates without switching their API version and without their integration getting broken.
