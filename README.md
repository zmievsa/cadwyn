# universi

Modern [Stripe-like](https://stripe.com/blog/api-versioning) API versioning for FastAPI

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
    AbstractVersionChange,
    convert_response_to_previous_version_for,
)

class ChangeAddressToList(AbstractVersionChange):
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

Finally, we group the version changes in the `Versions` class. This represents the different versions of your API and the changes between them. You can add any "version changes" to any version. For simplicity, let's use versions 2002 and 2001 which means that we had a single address in API in 2001 and added addresses as a list in 2002's version.

```python
from universi.structure import Version, Versions
from datetime import date

versions = Versions(
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

Obviously, this was just a simple example and universi has a lot more features so if you're interested -- take a look at the reference

## Reference

TBD
