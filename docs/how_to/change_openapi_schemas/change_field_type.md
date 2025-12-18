# Change field type in schemas

## Change the type incompatibly

If your data had a type `int` and you want to change it to a `str` in a new version, then your data from the new version can easily break the responses of the old versions, thus making it [data versioning](../../concepts/beware_of_data_versioning.md), not API versioning -- as you are versioning the fundamental structures the user is operating on instead of just the API.

## Expand the type

Suppose your clients could choose a `role` for your users. Originally, it was only possible to choose `admin` or `regular` but you would like to expand it to `moderator` which has all the permissions of an admin except assigning other admins.

This is not a breaking change in terms of requests but it [**can be**](#why-enum-expansion-is-a-breaking-change-for-responses) a breaking change in terms of responses. So if you do consider it a breaking change in terms of responses, do the following:

1. Add `moderator` value into `users.UserRoleEnum`
2. Add the following migration to `versions.v2001_01_01`:

```python
{! ./docs_src/how_to/change_openapi_schemas/change_field_type/block001.py !}
```

You convert moderators to regulars in older versions because it is a safer choice for your users.

### Why enum expansion is a breaking change for responses

Suppose your schema contained a list that contains euros and/or dollars. To unmarshal the JSON, Cadwyn takes the JSON string and tries to convert it to a list of euros and/or dollars. If there appears Georgian lari in the list, Cadwyn will fail to unmarshal such a list, which makes adding an enum value a breaking change for a list of items.

If an API client expects `Array<Euro | Dollar>`, then `Array<Euro>` and `Array<Dollar>` would both be considered compatible responses but `Array<Euro | Dollar | Lari>` would not. That happens because `Array<Euro | Dollar | Lari>` is a not a subtype of `Array<Euro | Dollar>` while `Array<Euro>` is.

In a sense, extending an enum that has `USD` with `USD | EUR` is equivalent to turning an `int` field into an `int | str` field, which is a breaking change. Hence extending an enum is often a breaking change and thus you might not need to solve this problem at all.

Additional resources:

* <https://github.com/OAI/OpenAPI-Specification/issues/1552>
* <https://users.rust-lang.org/t/solved-is-adding-an-enum-variant-a-breaking-change/26721/5>
* <https://github.com/graphql/graphql-js/issues/968>

In these sections, we'll be working with Cadwyn's user response model: `users.UserResource`. Note that the main question here is "Will I be able to serialize this change to any of my versions?" as any change to responses can make them incompatible with the data in your database.

## Narrow the type

Suppose that previously users could specify their date of birth as a datetime instead of a date. You are planning to change that. You can solve this by making it a datetime in HEAD version, converting it to date in latest version, and then making it a datetime again in the old versions. So whenever you receive a request in an old version, it will get converted to HEAD version where it is a datetime. And whenever you receive a request in latest version, it will also be converted to HEAD where date will simply be casted to datetime with time = 00:00:00.

0. Continue storing `date_of_birth` as a datetime in your database to avoid breaking any old behavior
1. Add the following migration to `versions.v2001_01_01` which will turn `date_of_birth` into a date in 2001_01_01. Note how the validator is used to make sure that `date_of_birth` is converted to date in the latest version. It is only necessary in Pydantic 2 because it has no implicit casting from datetime to date. Note also how strings are used for types: this is not always necessary; it just allows you to control how Cadwyn is going to render your types. Most of the time you won't need to use strings for types.

```python
{! ./docs_src/how_to/change_openapi_schemas/change_field_type/block002.py !}
```

The process above is a bit complex, so let us break it down:

1. `date_of_birth` field is a datetime in HEAD, a date in 2001, and a datetime again in 2000.
2. You need a way to keep the 2000 behavior making it unavailable for users in 2001. Cadwyn always converts all requests to the HEAD version so:
    * When user creation requests are received from 2001, they are converted directly to HEAD, and pydantic casts date to datetime without any issue
    * When user get requests are received from 2001, they are converted directly from HEAD to latest, and the validator casts datetime to date (note that pydantic 1 would be able to do it even without a validator)
    * When user creation requests are received from 2000, they are converted directly to HEAD. They have the same type for `date_of_birth`, so Cadwyn easily processes them
    * When user get requests are received from 2000, they are converted directly from HEAD to 2000. They have the same type for `date_of_birth`, so Cadwyn easily processes them

All of these interactions are done internally by Cadwyn. As you see, the process is more than straightforward: requests are converted to HEAD, and responses are converted from HEAD.

Thus, you have kept the old behavior, added new constrained behavior, and minimized the impact on your business logic as business logic simply doesn't know that `date_of_birth` in requests is ever a date and that `date_of_birth` in responses is ever a date. All of this information is hidden in your migration.

Please note that unlike schemas, routes, and business logic, the migrations written above will likely never need to change because they describe the fundamental differences between the API versions, and these differences cannot be changed in the future because that would defeat the purpose of API versioning. This makes migrations effectively immutable and consequently very cheap to support.
