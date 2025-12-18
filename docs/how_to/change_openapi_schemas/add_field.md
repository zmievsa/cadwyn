# Add a field to OpenAPI schemas

## To response schema

Let's say that we decided to expose the creation date of user's account with a `created_at` field in our API. This is **not** a breaking change so a new version is completely unnecessary. However, if you believe that you absolutely have to make a new version, then you can simply follow the recommended approach below but add a version change with [field didn't exist instruction](../../concepts/schema_migrations.md#remove-a-field-from-the-older-version).

You just need to add `created_at` field into `users.UserResource`.

Now you have everything you need at your disposal: field `created_at` is available in all versions and your users do not even need to do any extra actions. Just make sure that the data for it is available in all versions too. If it's not: make the field optional.

## To both request and response schemas

### Field is optional

Let's say we want our users to be able to specify a middle name but it is nullable. It is not a breaking change so no new version is necessary whether it is requests or responses.

You just need to add a nullable `middle_name` field into `users.BaseUser` as if you were working with a barebones FastAPI app.

### Field is required

#### With compatible default value in older versions

Let's say that our users had a field `country` that defaulted to `USA` but our product is now used well beyond United States so we want to make this field required in the HEAD version.

1. Remove `default="US"` from `users.UserCreateRequest`
2. Add the following migration to `versions.v2001_01_01`:

```python
{! ./docs_src/how_to/change_openapi_schemas/add_field/block001.py !}
```

That's it. Our old schemas will now contain a default but in HEAD country will be required. You might notice a weirdness: if we set a default in the old version, why would we also write a migration? That's because of a sad implementation detail of pydantic that [prevents us](../../concepts/schema_migrations.md#change-a-field-in-the-older-version) from using defaults from old versions.

#### With incompatible default value in older versions

Let's say that we want to add a required field `phone` to our users. However, older versions did not have such a field at all. This means that the field is going to be nullable (or nonexistent) in the old versions but required in the HEAD version. This also means that older versions contain a wider type (`str | None`) than the HEAD version (`str`). So when we try to migrate request bodies from the older versions to HEAD -- we might receive a `ValidationError` because `None` is not an acceptable value for `phone` field in the new version. Whenever we have a problem like this, when older version contains more data or a wider type set of data,  we can simply define a wider type in our HEAD version and then narrow it in latest.

So we will make `phone` nullable in HEAD, then make it required in `latest`, and then make it nullable again in older versions, thus making it possible to convert all of our requests to HEAD.

1. Add `phone` field of type `str | None` to `users.BaseUser`
2. Add `phone` field of type `str | None` with a `default=None` to `users.UserResource` because all users created with older versions of our API won't have phone numbers.
3. Add the following migrations to `versions.v2001_01_01`:

```python
{! ./docs_src/how_to/change_openapi_schemas/add_field/block002.py !}
```

See how we didn't remove the `phone` field from old versions? Instead, we allowed a nullable `phone` field to be passed into both old `UserResource` and old `UserCreateRequest`. This gives our users new functionality without needing to update their API version. It is one of the best parts of Cadwyn's approach: our users can get years worth of updates without switching their API version and without their integration getting broken.
