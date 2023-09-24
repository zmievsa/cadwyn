# Universi

Modern [Stripe-like](https://stripe.com/blog/api-versioning) API versioning in FastAPI

---

<p align="center">
<a href="https://github.com/ovsyanka83/universi/actions?query=workflow%3ATests+event%3Apush+branch%3Amain" target="_blank">
    <img src="https://github.com/Ovsyanka83/universi/actions/workflows/test.yaml/badge.svg?branch=main&event=push" alt="Test">
</a>
<a href="https://codecov.io/gh/ovsyanka83/universi" target="_blank">
    <img src="https://img.shields.io/codecov/c/github/ovsyanka83/universi?color=%2334D058" alt="Coverage">
</a>
<a href="https://pypi.org/project/universi/" target="_blank">
    <img alt="PyPI" src="https://img.shields.io/pypi/v/universi?color=%2334D058&label=pypi%20package" alt="Package version">
</a>
<a href="https://pypi.org/project/universi/" target="_blank">
    <img src="https://img.shields.io/pypi/pyversions/universi?color=%2334D058" alt="Supported Python versions">
</a>
</p>

## Installation

```bash
pip install universi
```

## Who is this for?

Universi allows you to support a single version of your code, auto-generating the code/routes for older versions. You keep versioning encapsulated in small and independent "version change" modules while your business logic knows nothing about versioning.

Its approach will be useful if you want to:

1. Support many API versions for a long time
2. Effortlessly backport features and bugfixes to all of your versions

Otherwise, more conventional methods of API versioning may be preferable.

## Tutorial

This guide provides a step-by-step tutorial for setting up automatic API versioning using Universi library. I will illustrate this with an example of a User API, where we will be implementing changes to a User's address.

### A dummy setup

Here is an initial API setup where the User has a single address. We will be implementing two routes - one for creating a user and another for retrieving user details. We'll be using "int" for ID for simplicity.

The first API you come up with usually doesn't require more than one address -- why bother?

So we create our file with schemas:

```python
from pydantic import BaseModel


class UserCreateRequest(BaseModel):
    address: str


class UserResource(BaseModel):
    id: int
    address: str
```

And we create our file with routes:

```python
from versions.latest.users import UserCreateRequest, UserResource
from universi import VersionedAPIRouter

router = VersionedAPIRouter()


@router.post("/users", response_model=UserResource)
async def create_user(payload: UserCreateRequest):
    return {
        "id": 83,
        "address": payload.address,
    }


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: int):
    return {
        "id": user_id,
        "address": "123 Example St",
    }
```

### Turning address into a list

During our development, we have realized that the initial API design was wrong and that addresses should have always been a list because the user wants to have multiple addresses to choose from so now we have to change the type of the "address" field to the list of strings.

```python
from pydantic import BaseModel
from pydantic import Field


class UserCreateRequest(BaseModel):
    addresses: list[str] = Field(min_items=1)


class UserResource(BaseModel):
    id: int
    addresses: list[str]
```

```python
@router.post("/users", response_model=UserResource)
async def create_user(payload: UserCreateRequest):
    return {
        "id": 83,
        "addresses": payload.addresses,
    }


@router.get("/users/{user_id}", response_model=UserResource)
async def get_user(user_id: int):
    return {
        "id": user_id,
        "addresses": ["123 Example St", "456 Main St"],
    }
```

But every user of ours will now have their API integration broken. To prevent that, we have to introduce API versioning. There aren't many methods of doing that. Most of them force you to either duplicate your schemas, your endpoints, or your entire app instance. And it makes sense, really: duplication is the only way to make sure that you will not break old versions with your new versions; the bigger the piece you duplicating -- the safer. Of course, the safest being duplicating the entire app instance and even having a separate database. But that is expensive and makes it either impossible to make breaking changes often or to support many versions. As a result, either you need infinite resources, very long development cycles, or your users will need to often migrate from version to version.

Stripe has come up [with a solution](https://stripe.com/blog/api-versioning): let's have one latest app version whose responses get migrated to older versions and let's describe changes between these versions using migrations. This approach allows them to keep versions for **years** without dropping them. Obviously, each breaking change is still bad and each version still makes our system more complex and expensive, but their approach gives us a chance to minimize that. Additionally, it allows us backport features and bugfixes to older versions. However, you will also be backporting bugs, which is a sad consequence of eliminating duplication.

Universi is heavily inspired by this approach so let's continue our tutorial and now try to combine the two versions we created using versioning.

### Creating the Migration

We need to create a migration to handle changes between these versions. For every endpoint whose `response_model` is `UserResource`, this migration will convert the list of addresses back to a single address when migrating to the previous version. Yes, migrating **back**: you might be used to database migrations where we write upgrade migration and downgrade migration but here our goal is to have an app of latest version and to describe what older versions looked like in comparison to it. That way the old versions are frozen in migrations and you can **almost** safely forget about them.

```python
from pydantic import Field
from universi.structure import (
    schema,
    VersionChange,
    convert_response_to_previous_version_for,
)


class ChangeAddressToList(VersionChange):
    description = (
        "Change user address to a list of strings to "
        "allow the user to specify multiple addresses"
    )
    instructions_to_migrate_to_previous_version = [
        # You should use schema inheritance if you don't want to repeat yourself in such cases
        schema(UserCreateRequest).field("addresses").didnt_exist,
        schema(UserCreateRequest).field("address").existed_as(type=str, info=Field()),
        schema(UserResource).field("addresses").didnt_exist,
        schema(UserResource).field("address").existed_as(type=str, info=Field()),
    ]

    @convert_response_to_previous_version_for(UserResource)
    def change_addresses_to_single_item(payload: dict[str, Any]) -> None:
        data["address"] = data.pop("addresses")[0]

    # Note that this approach is outdated and it is now recommended to use request migrations
    @schema(UserCreateRequest).property("addresses").was
    def addresses_property(parsed_schema):
        return [parsed_schema.address]
```

See how we are popping the first address from the list? This is only guaranteed to be possible because we specified earlier that `min_items` for `addresses` must be `1`. If we didn't, then the user would be able to create a user in a newer version that would be impossible to represent in the older version. I.e. If anyone tried to get that user from the older version, they would get a `ResponseValidationError` because the user wouldn't have data for a mandatory `address` field. You need to always keep in mind tht API versioning is only for versioning your **API**, your interface. Your versions must still be completely compatible in terms of data. If they are not, then you are versioning your data and you should really go with a separate app instance. Otherwise, your users will have a hard time migrating back and forth between API versions and so many unexpected errors.

See how we added the `addresses` property? This simple instruction will allow us to use `addresses` even from the old schema, which means that our api route will not need to know anything about versioning. The main goal of universi is to shift the logic of versioning away from your business logic and api endpoints which makes your project easier to navigate and which makes deleting versions a breeze.

### Grouping Version Changes

Finally, we group the version changes in the `VersionBundle` class. This represents the different versions of your API and the changes between them. You can add any "version changes" to any version. For simplicity, let's use versions 2002 and 2001 which means that we had a single address in API in 2001 and added addresses as a list in 2002's version.

```python
from universi.structure import Version, VersionBundle
from datetime import date
from contextvars import ContextVar

api_version_var = ContextVar("api_version_var")

versions = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressToList),
    Version(date(2001, 1, 1)),
    api_version_var=api_version_var,
)
```

That's it. You're done with describing things. Now you just gotta ask universi to do the rest for you. We'll need the VersionedAPIRouter we used previously, our API versions, and the module representing the latest versions of our schemas.

```python
from versions import latest, api_version_var
from universi import regenerate_dir_to_all_versions, generate_all_router_versions

regenerate_dir_to_all_versions(latest, versions)
router_versions = generate_all_router_versions(
    router,
    versions=versions,
    latest_schemas_module=latest,
)
api_version_var.set(date(2002, 1, 1))
uvicorn.run(router_versions[date(2002, 1, 1)])
```

Universi has generated multiple things in this code:

* Three versions of our schemas: one for each API version and one that includes definitions of unions of all versions for each schema which will be useful when you want to type check that you are using requests of different versions correctly. For example, we'll have `UserCreateRequest` defined there which is a `TypeAlias` pointing to the union of 2002 version and 2001 version of `UserCreateRequest`.
* Two versions of our API router: one for each API version

You can now just pick a router by its version and run it separately or use a parent router/app to specify the logic by which you'd like to pick a version. I recommend using a header-based router with version dates as headers. And yes, that's how Stripe does it.

Note that universi migrates your response data based on the `api_version_var` context variable so you must set it with each request. `universi.get_universi_dependency` does that for you automatically on every request based on header value.

Obviously, this was just a simple example and universi has a lot more features so if you're interested -- take a look at the reference.

### Examples

Please, see [tutorial examples](https://github.com/Ovsyanka83/universi/tree/main/tests/test_tutorial) for the fully working version of the project above.

## Important warnings

1. The goal of Universi is to **minimize** the impact of versioning on your business logic. It provides all necessary tools to prevent you from **ever** checking for a concrete version in your code. So please, if you are tempted to check something like `api_version_var.get() >= date(2022, 11, 11)` -- please, take another look into [reference](#version-changes-with-side-effects) section. I am confident that you will find a better solution there.
2. Universi does not include a header-based router like FastAPI. We hope that soon a framework for header-based routing will surface which will allow universi to be a full versioning solution.
3. I ask you to be very detailed in your descriptions for version changes. Spending these 5 extra minutes will potentially save you tens of hours in the future when everybody forgets when, how, and why the version change was made.
4. We migrate responses backwards in versions from the latest version using data migration functions and requests forward in versions until the latest version using properties on pydantic models.
5. Universi doesn't edit your imports when generating schemas so if you make any imports from versioned code to versioned code, I would suggest using [relative imports](https://docs.python.org/3/reference/import.html#package-relative-imports) to make sure that they will still work as expected after code generation.

## Reference

### Endpoints

Note that the endpoint constructor contains a second argument that describes the methods of the endpoints you would like to edit. If you have two routes for a single endpoint and you put both of their methods into the instruction -- both of them are going to be changed as you would expect.

#### Defining endpoints that didn't exist in new versions

If you had an endpoint in old version but do not have it in a new one, you must still define it but mark it as deleted.

```python
@router.only_exists_in_older_versions
@router.get("/my_old_endpoint")
async def my_old_endpoint():
    ...
```

and then define it as existing in one of the older versions:

```python
from universi.structure import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        endpoint("/my_old_endpoint", ["GET"]).existed,
    ]
```

#### Defining endpoints that didn't exist in old versions

If you have an endpoint in your new version that must not exist in older versions, you define it as usual and then mark it as "non-existing" in old versions:

```python
from universi.structure import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        endpoint("/my_new_endpoint", ["GET"]).didnt_exist,
    ]
```

#### Changing endpoint attributes

If you want to change any attribute of your endpoint in a new version, you can return the attribute's value in all older versions like so:

```python
from universi.structure import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        endpoint("/my_endpoint", ["GET"]).had(description="My old description"),
    ]
```

#### Dealing with endpoint duplicates

Sometimes, when you're doing some advanced changes in between versions, you will need to rewrite your endpoint function entirely. So essentially you'd have the following structure:

```python
from fastapi.params import Param
from fastapi.headers import Header
from typing import Annotated
from universi import VersionedAPIRouter

router = VersionedAPIRouter()


@router.only_exists_in_older_versions
@router.get("/users")
def get_users_by_name_before_we_started_using_params(
    user_name: Annotated[str, Header()]
):
    """Do some logic with user_name"""


@router.get("/users")
def get_users_by_name(user_name: Annotated[str, Param()]):
    """Do some logic with user_name"""
```

As you see, these two functions have the same methods and paths. And when you have many versions, you can have even more functions like these two. So how do we ask universi to restore only one of them and delete the other one?

```python
from universi.structure import VersionChange, endpoint


class UseParamsInsteadOfHeadersForUserNameFiltering(VersionChange):
    description = (
        "Use params instead of headers for user name filtering in GET /users "
        "because using headers is a bad API practice in such scenarios."
    )
    instructions_to_migrate_to_previous_version = [
        # We don't have to specify the name here because there's only one such deleted endpoint
        endpoint("/users", ["GET"]).existed,
        # We do have to specify the name because we now have two existing endpoints after the instruction above
        endpoint("/users", ["GET"], func_name="get_users_by_name").didnt_exist,
    ]
```

So by using a more concrete `func_name`, we are capable to distinguish between different functions that affect the same routes.

### Enums

#### Adding enum members

Note that adding enum members **can** be a breaking change unlike adding optional fields to a schema. For example, if I return a list of entities, each of which has some type, and I add a new type -- then my client's code is likely to break.

So I suggest adding enum members in new versions as well.

```python
from universi.structure import VersionChange, enum
from enum import auto


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        enum(my_enum).had(foo="baz", bar=auto()),
    ]
```

#### Removing enum members

```python
from universi.structure import VersionChange, enum


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        enum(my_enum).didnt_have("foo", "bar"),
    ]
```

### Schemas

#### Add a field

```python
from pydantic import Field
from universi.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema)
        .field("foo")
        .existed_as(type=list[str], info=Field(description="Foo")),
    ]
```

You can also specify any string in place of type:

```python
schema(MySchema).field("foo").existed_as(type="AnythingHere")
```

It is often the case that you want to add a type that has not been imported in your schemas yet. You can use `import_from` and optionally `import_as` to do this:

```python
schema(MySchema).field("foo").existed_as(
    type=MyOtherSchema, import_from="..some_module", import_as="Foo"
)
```

Which will render as:

```python
from ..some_module import MyOtherSchema as Foo
from pydantic import BaseModel, Field


class MySchema(BaseModel):
    foo: Foo = Field()
```

#### Remove a field

```python
from universi.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).field("foo").didnt_exist,
    ]
```

#### Change a field

```python
from universi.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).field("foo").had(description="Foo"),
    ]
```

#### Add a property

```python
from universi.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = []

    @schema(MySchema).property("foo").was
    def any_name_here(parsed_schema):
        return parsed_schema.some_other_field
```

#### Remove a property

```python
from universi.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).property("foo").didnt_exist,
    ]
```

#### Rename a schema

If you wish to rename your schema to make sure that its name is different in openapi.json:

```python
from universi.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).had(name="OtherSchema"),
    ]
```

which will replace all references to this schema with the new name.

### Unions

As you probably realize, when you have many versions with different request schemas and your business logic receives one of them -- you're in trouble. You could handle them all separately by checking the version of each schema and then using the correct logic for it but universi tries to offer something better.

Instead, we take a union of all of our request schemas and write our business logic as if it receives that union. For example, if version 2000 had field "foo" of type `str` and then version 2001 changed that field to type `int`, then a union of these schemas will have foo as `str | int` so your type checker will protect you against incorrect usage. Same goes for added/deleted fields. Obviously, manually importing all your schemas and then taking a union of them is tough, especially if you have many versions, which is why Universi not only generates a directory for each of your versions, but it also generates a "unions" directory that contains unions of all your schemas and enums.

For example, if we had a schema named `MySchema` and two versions of it: 2000 and 2001, then the union definition will look like the following:

```python
from ..latest import my_schema_module as latest_my_schema_module
from ..v2000_01_01 import my_schema_module as v2000_01_01_my_schema_module
from ..v2001_01_01 import my_schema_module as v2001_01_01_my_schema_module

MySchema = (
    latest_my_schema_module.MySchema
    | v2000_01_01_my_schema_module.MySchema
    | v2001_01_01_my_schema_module.MySchema
)
```

and you would be able to use it like so:

```python
from src.schemas.unions.my_schema_module import MySchema


async def the_entrypoint_of_my_business_logic(request_payload: MySchema):
    ...
```

Note that this feature only affects type checking and does not affect your functionality. Note also that every [request migration](#request-data-migration) that affects a specific body schema will automatically make Universi omit the body schema from  that version in unions.

### Data migration

#### Response data migration

As described in the tutorial, universi can convert your response data into older versions. It does so by running your "migration" functions whenever it encounters a version change:

```python
from universi.structure import VersionChange, convert_response_to_previous_version_for
from typing import Any


class ChangeAddressToList(VersionChange):
    description = "..."

    @convert_response_to_previous_version_for(MyEndpointResponseModel)
    def change_addresses_to_single_item(response: ResponseInfo) -> None:
        response.body["address"] = response.body.pop("addresses")[0]
```

It is done by applying a versioning decorator to each endpoint with the given `response_model` which automatically detects the API version by getting it from the [contextvar](#api-version-header-and-context-variables) and applying all version changes until the selected version in reverse. Note that if the version is not set, then no changes will be applied.

#### Request data migration

```python
from universi.structure import VersionChange, convert_request_to_next_version_for
from typing import Any
from my_schemas.latest import UserCreateRequest


class ChangeAddressToList(VersionChange):
    description = "..."

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_addresses_to_single_item(request: RequestInfo) -> None:
        request.body["addresses"] = [request.body.pop("address")]
```

### Version changes with side effects

Sometimes you will use API versioning to handle a breaking change in your **business logic**, not in the schemas themselves. In such cases, it is tempting to add a version check and just follow the new business logic such as:

```python
if api_version_var.get() >= date(2022, 11, 11):
    # do new logic here
    ...
```

In universi, this approach is highly discouraged. It is recommended that you avoid side effects like this at any cost because each one makes your core logic harder to understand. But if you cannot, then I urge you to at least abstract away versions and versioning from your business logic which will make your code much easier to read.

To simplify this, universi has a special `VersionChangeWithSideEffects` class. It makes finding dangerous versions that have side effects much easier and provides a nice abstraction for checking whether we are on a version where these side effects have been applied.

As an example, let's use the tutorial section's case with the user and their address. Let's say that we use an external service to check whether user's address is listed in it and return 400 response if it is not. Let's also say that we only added this check in the newest version.

```python
from universi.structure import VersionChangeWithSideEffects


class UserAddressIsCheckedInExternalService(VersionChangeWithSideEffects):
    description = (
        "User's address is now checked for existense in an external service. "
        "If it doesn't exist there, a 400 code is returned."
    )
```

Then we will have the following check in our business logic:

```python
from src.versions import versions, UserAddressIsCheckedInExternalService


async def create_user(payload):
    if UserAddressIsCheckedInExternalService.is_applied:
        check_user_address_exists_in_an_external_service(payload.address)
    ...
```

So this change can be contained in any version -- your business logic doesn't know which version it has and shouldn't.

### API Version header and context variables

Universi automatically converts your data to a correct version and has "version checks" when dealing with side effects as described in [the section above](#version-changes-with-side-effects). It can only do so using a special [context variable](https://docs.python.org/3/library/contextvars.html) that stores the current API version.

Use `universi.get_universi_dependency` to get a `fastapi.Depends` that automatically sets this contextvar based on a header name that you pick.

You can also set the variable yourself or even pass a different compatible contextvar to your `universi.VersionBundle` constructor.

## Similar projects

The following projects are trying to accomplish similar results with a lot more simplistic functionality.

* <https://github.com/sjkaliski/pinned>
* <https://github.com/phillbaker/gates>
* <https://github.com/lukepolo/laravel-api-migrations>
* <https://github.com/tomschlick/request-migrations>
* <https://github.com/keygen-sh/request_migrations>
