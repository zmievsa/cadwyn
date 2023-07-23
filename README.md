# universi

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

<!---
# TODO: Note that we don't handle "from .schemas import Schema as OtherSchema" case
# TODO: Need to validate that the user doesn't use versioned schemas instead of the latest ones
-->

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
from universi import Field


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

We need to create a migration to handle changes between these versions. This migration will convert the list of addresses back to a single address when migrating to the previous version. Yes, migrating **back**: you might be used to database migrations where we write upgrade migration and downgrade migration but here our goal is to have an app of latest version and to describe what older versions looked like in comparison to it. That way the old versions are frozen in migrations and you can **almost** safely forget about them.

```python
from universi import Field
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
    instructions_to_migrate_to_previous_version = (
        # You should use schema inheritance if you don't want to repeat yourself in such cases
        schema(UserCreateRequest).field("addresses").didnt_exist,
        schema(UserCreateRequest).field("address").existed_with(type=str, info=Field()),
        schema(UserResource).field("addresses").didnt_exist,
        schema(UserResource).field("address").existed_with(type=str, info=Field()),
    )

    @convert_response_to_previous_version_for(get_user, create_user)
    def change_addresses_to_single_item(cls, data: dict[str, Any]) -> None:
        data["address"] = data.pop("addresses")[0]
    
    @schema(UserCreateRequest).had_property("addresses")
    def addresses_property(parsed_schema):
        return [parsed_schema.address]

```

s
See how we are popping the first address from the list? This is only guaranteed to be possible because we specified earlier that `min_items` for `addresses` must be `1`. If we didn't, then the user would be able to create a user in a newer version that would be impossible to represent in the older version. I.e. If anyone tried to get that user from the older version, they would get a `ResponseValidationError` because the user wouldn't have data for a mandatory `address` field. You need to always keep in mind tht API versioning is only for versioning your **API**, your interface. Your versions must still be completely compatible in terms of data. If they are not, then you are versioning your data and you should really go with a separate app instance. Otherwise, your users will have a hard time migrating back and forth between API versions and so many unexpected errors.

See how we added the `addresses` property? This simple instruction will allow us to use `addresses` even from the old schema, which means that our api route will not need to know anything about versioning. The main goal of universi is to shift the logic of versioning away from your business logic and api endpoints which makes your project easier to navigate and which makes deleting versions a breeze.

### Grouping Version Changes

Finally, we group the version changes in the `VersionBundle` class. This represents the different versions of your API and the changes between them. You can add any "version changes" to any version. For simplicity, let's use versions 2002 and 2001 which means that we had a single address in API in 2001 and added addresses as a list in 2002's version.

```python
from universi.structure import Version, VersionBundle
from datetime import date

versions = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressToList),
    Version(date(2001, 1, 1)),
)
```

That's it. You're done with describing things. Now you just gotta ask universi to do the rest for you. We'll need the VersionedAPIRouter we used previously, our API versions, and the module representing the latest versions of our schemas.

```python
from versions import latest
from universi import regenerate_dir_to_all_versions, api_version_var

regenerate_dir_to_all_versions(latest, versions)
router_versions = router.create_versioned_copies(
    versions,
    latest_schemas_module=latest,
)
api_version_var.set(date(2002, 1, 1))
uvicorn.run(router_versions[date(2002, 1, 1)])
```

Universi has generated multiple things in this code:

* Three versions of our schemas: one for each API version and one that includes definitions of unions of all versions for each schema which will be useful when you want to type check that you are using requests of different versions correctly. For example, we'll have `UserCreateRequestUnion` defined there which is a `TypeAlias` pointing to the union of 2002 version and 2001 version of `UserCreateRequest`.
* Two versions of our API router: one for each API version

You can now just pick a router by its version and run it separately or use a parent router/app to specify the logic by which you'd like to pick a version. I recommend using a header-based router with version dates as headers. And yes, that's how Stripe does it.

Note that universi migrates your response data based on the api_version_var context variable so you must set it with each request. `universi.header` has a dependency that does that for you.

Obviously, this was just a simple example and universi has a lot more features so if you're interested -- take a look at the reference.

### Examples

Please, see [tutorial examples](https://github.com/Ovsyanka83/universi/tree/main/tests/test_tutorial) for the fully working version of the project above.

## Important warnings

1. The goal of Universi is to **minimize** the impact of versioning on your business logic. It provides all necessary tools to prevent you from **ever** checking for a concrete version in your code. So please, if you are tempted to check something like `api_version_var.get() >= date(2022, 11, 11)` -- please, take another look into [reference](#version-changes-with-side-effects) section. I am confident that you will find a better solution there.
2. Universi uses its own `universi.Field` function for defining pydantic fields. If you want your pydantic schemas to migrate correctly, then you must use `universi.Field` instead of `pydantic.Field` everywhere because `pydantic.Field` does not preserve the information about which attributes were passed and which were not so code generation is much harder with it.
3. Universi does not include a header-based router like FastAPI. We hope that soon a framework for header-based routing will surface which will allow universi to be a full versioning solution.
4. I ask you to be very detailed in your descriptions for version changes. Spending these 5 extra minutes will potentially save you tens of hours in the future when everybody forgets when, how, and why the version change was made.
5. We migrate responses backwards in versions from the latest version using data migration functions and requests forward in versions until the latest version using properties on pydantic models.

## Reference

### Endpoints

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
    instructions_to_migrate_to_previous_version = (
        endpoint(my_old_endpoint).existed,
    )

```

#### Defining endpoints that didn't exist in old versions

If you have an endpoint in your new version that must not exist in older versions, you define it as usual and then mark it as "non-existing" in old versions:

```python
from universi.structure import VersionChange, endpoint

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint(my_new_endpoint).didnt_exist,
    )

```

#### Changing endpoint attributes

If you want to change any attribute of your endpoint in a new version, you can return the attribute's value in all older versions like so:

```python
from universi.structure import VersionChange, endpoint

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint(my_endpoint).had(description="My old description"),
    )

```

### Enums

#### Adding enum members

Note that adding enum members **can** be a breaking change unlike adding optional fields to a schema. For example, if I return a list of entities, each of which has some type, and I add a new type -- then my client's code is likely to break.

So I suggest adding enum members in new versions as well.

```python
from universi.structure import VersionChange, enum
from enum import auto

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        enum(my_enum).had(foo="baz", bar=auto()),
    )

```

#### Removing enum members

```python
from universi.structure import VersionChange, enum

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        enum(my_endpoint).didnt_have("foo", "bar"),
    )

```

### Schemas

#### Add a field

```python
from universi import Field
from universi.structure import VersionChange, schema

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").existed_with(type=list[str], info=Field(description="Foo")),
    )

```

#### Remove a field

```python
from universi.structure import VersionChange, schema

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").didnt_exist,
    )

```

#### Change a field

```python
from universi.structure import VersionChange, schema

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(description="Foo"),
    )

```

#### Add a property

```python
from universi.structure import VersionChange, schema

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = ()

    @schema(MySchema).had_property("foo")
    def any_name_here(parsed_schema):
        # Anything can be returned from here
        return parsed_schema.some_other_field

```

#### Remove a property

```python
from universi.structure import VersionChange, schema

class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).property("foo").didnt_exist,
    )

```

### Unions

As you probably realize, when you have many versions with different request schemas and your business logic receives one of them -- you're in trouble. You could handle them all separately by checking the version of each schema and then using the correct logic for it but universi tries to offer something better.

Instead, we take a union of all of our request schemas and write our business logic as if it receives that union. For example, if version 2000 had field "foo" of type `str` and then version 2001 changed that field to type `int`, then a union of these schemas will have foo as `str | int` so your type checker will protect you against incorrect usage. Same goes for added/deleted fields. Obviously, manually importing all your schemas and then taking a union of them is tough, especially if you have many versions, which is why Universi not only generates a directory for each of your versions, but it also generates a "unions" directory that contains unions of all your schemas and enums.

For example, if we had a schema named `MySchema` and two versions of it: 2000 and 2001, then the union definition will look like the following:

```python
import src.versions.v2000_01_01.my_schema_module
import src.versions.v2001_01_01.my_schema_module
import src.versions.latest.my_schema_module

MySchemaUnion = (
    versions.v2000_01_01.my_schema_module.MySchema |
    versions.v2001_01_01.my_schema_module.MySchema |
    versions.latest.my_schema_module.MySchema
)
```

and you would be able to use it like so:

```python
from src.versions.unions.my_schema_module import MySchemaUnion

async def the_entrypoint_of_my_business_logic(request_payload: MySchemaUnion):
    ...

```

Note that this feature only affects type checking and does not affect your functionality.

### Data conversion

As described in the tutorial, universi can convert your response data into older versions. It does so by running your "migration" functions whenever it encounters a version change:

```python
from universi.structure import VersionChange, convert_response_to_previous_version_for
from typing import Any

class ChangeAddressToList(VersionChange):
    description = "..."

    @convert_response_to_previous_version_for(my_endpoint, my_other_endpoint)
    def change_addresses_to_single_item(cls, data: dict[str, Any]) -> None:
        data["address"] = data.pop("addresses")[0]

```

It is done by applying `universi.VersionBundle.versioned(...)` decorator to each endpoint which automatically detects the API version by getting it from the [contextvar](#api-version-header-and-context-variables) and applying all version changes until the selected version in reverse. Note that if the version is not set, then no changes will be applied.

If you want to convert a specific response to a specific version, you can use `universi.VersionBundle.data_to_version(...)`.

### Version changes with side effects

Sometimes you will use API versioning to handle a breaking change in your **business logic**, not in the schemas themselves. In such cases, it is tempting to add a version check and just follow the new business logic such as:

```python
if api_version_var.get() >= date(2022, 11, 11):
    # do new logic here
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
    if UserAddressIsCheckedInExternalService.is_active:
        check_user_address_exists_in_an_external_service(payload.address)
    ...
```

So this change can be contained in any version -- your business logic doesn't know which version it has and shouldn't.

### API Version header and context variables

Universi automatically converts your data to a correct version and has "version checks" when dealing with side effects as described in [the section above](#version-changes-with-side-effects). It can only do so using a special [context variable](https://docs.python.org/3/library/contextvars.html) that stores the current API version.

Universi has such default variable defined as `universi.api_version_var`. You can also use `universi.get_universi_dependency` to get a `fastapi.Depends` that automatically sets this contextvar based on a header name that you pick.

You can also set the variable yourself or even pass a different compatible contextvar to your `universi.VersionBundle` constructor.
