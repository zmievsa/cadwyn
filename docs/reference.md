
# Reference

Cadwyn aims to be the most accurate API Versioning model out there. First of all, you maintain **zero** duplicated code yourself. Usually, in API versioning you [would need to](./theory.md) duplicate and maintain at least some layer of your applicaton. It could be the database, business logic, schemas, and endpoints. Cadwyn only duplicates your:

* schemas but you do not maintain the duplicates -- you only regenerate it when necessary
* endpoints but only in runtime so you do not need to maintain the duplicates

The workflow is fairly straighforward:

1. Define a [VersionBundle](...) where you add your first version
2. Create a `data` directory with your latest schemas

## CLI

Cadwyn has an optional CLI interface that can be installed with `pip install cadwyn[cli]`.
Run `cadwyn --version` to check current version of Cadwyn.

## Code generation

Cadwyn generates versioned schemas and everything related to them from latest version. These versioned schemas will be automatically used in requests and responses for [versioned API routes](...). There are two methods of generating code: using a function and using the CLI:

### Function interface

You can use `cadwyn.generate_code_for_versioned_packages` which accepts a `template_module` (a directory which contains the latest versions) and `versions` which is the `VersionBundle` from which to generate versions.

### CLI interface

The interface is the same to the function one and is a shorthand for simple cases:

* `cadwyn generate-code-for-versioned-packages path.to.latest.package path.to.version.bundle:my_version_bundle`
* `cadwyn generate-code-for-versioned-packages path.to.latest.package path.to.version.bundle:func_that_returns_version_bundle`

#### **Note**

* You wouldn't use the system path style for both arguments. Instead, imagine that you are importing these modules in python -- that's the way you want to write down the paths.
* Take a look at how we point to our version bundle. We use ":" to say that it's a variable within the specified module

## Endpoints

Note that the endpoint constructor contains a second argument that describes the methods of the endpoints you would like to edit. If you have two routes for a single endpoint and you put both of their methods into the instruction -- both of them are going to be changed as you would expect.

### Defining endpoints that didn't exist in new versions

If you had an endpoint in old version but do not have it in a new one, you must still define it but mark it as deleted.

```python
@router.only_exists_in_older_versions
@router.get("/my_old_endpoint")
async def my_old_endpoint():
    ...
```

and then define it as existing in one of the older versions:

```python
from cadwyn.structure import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        endpoint("/my_old_endpoint", ["GET"]).existed,
    ]
```

### Defining endpoints that didn't exist in old versions

If you have an endpoint in your new version that must not exist in older versions, you define it as usual and then mark it as "non-existing" in old versions:

```python
from cadwyn.structure import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        endpoint("/my_new_endpoint", ["GET"]).didnt_exist,
    ]
```

### Changing endpoint attributes

If you want to change any attribute of your endpoint in a new version, you can return the attribute's value in all older versions like so:

```python
from cadwyn.structure import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        endpoint("/my_endpoint", ["GET"]).had(description="My old description"),
    ]
```

### Dealing with endpoint duplicates

Sometimes, when you're doing some advanced changes in between versions, you will need to rewrite your endpoint function entirely. So essentially you'd have the following structure:

```python
from fastapi.params import Param
from fastapi.headers import Header
from typing import Annotated
from cadwyn import VersionedAPIRouter

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

As you see, these two functions have the same methods and paths. And when you have many versions, you can have even more functions like these two. So how do we ask cadwyn to restore only one of them and delete the other one?

```python
from cadwyn.structure import VersionChange, endpoint


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

## Enums

All of the following instructions affect only code generation.

### Adding enum members

Note that adding enum members **can** be a breaking change unlike adding optional fields to a schema. For example, if I return a list of entities, each of which has some type, and I add a new type -- then my client's code is likely to break.

So I suggest adding enum members in new versions as well.

```python
from cadwyn.structure import VersionChange, enum
from enum import auto


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        enum(my_enum).had(foo="baz", bar=auto()),
    ]
```

### Removing enum members

```python
from cadwyn.structure import VersionChange, enum


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        enum(my_enum).didnt_have("foo", "bar"),
    ]
```

## Schemas

All of the following instructions affect only code generation.

### Add a field

```python
from pydantic import Field
from cadwyn.structure import VersionChange, schema


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

### Remove a field

```python
from cadwyn.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).field("foo").didnt_exist,
    ]
```

### Change a field

```python
from cadwyn.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).field("foo").had(description="Foo"),
    ]
```

### Rename a schema

If you wish to rename your schema to make sure that its name is different in openapi.json:

```python
from cadwyn.structure import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(MySchema).had(name="OtherSchema"),
    ]
```

which will replace all references to this schema with the new name.

## Data migration

### Response data migration

As described in the tutorial, cadwyn can convert your response data into older versions. It does so by running your "migration" functions whenever it encounters a version change:

```python
from cadwyn.structure import VersionChange, convert_response_to_previous_version_for
from typing import Any


class ChangeAddressToList(VersionChange):
    description = "..."

    @convert_response_to_previous_version_for(MyEndpointResponseModel)
    def change_addresses_to_single_item(response: ResponseInfo) -> None:
        response.body["address"] = response.body.pop("addresses")[0]
```

It is done by applying a versioning decorator to each endpoint with the given `response_model` which automatically detects the API version by getting it from the [contextvar](#api-version-header-and-context-variables) and applying all version changes until the selected version in reverse. Note that if the version is not set, then no changes will be applied.

### Request data migration

```python
from cadwyn.structure import VersionChange, convert_request_to_next_version_for
from typing import Any
from my_schemas.latest import UserCreateRequest


class ChangeAddressToList(VersionChange):
    description = "..."

    @convert_request_to_next_version_for(UserCreateRequest)
    def change_addresses_to_single_item(request: RequestInfo) -> None:
        request.body["addresses"] = [request.body.pop("address")]
```

### Internal request schemas

Let's say our `CreateUserRequest` had a field `email` which was `str | None` in version 1 but became a required `str` in version 2. How do we migrate our request from version 1 to version 2? The schema from version 2 will simply raise an error if you try to put a `None` into the `email` field.

That's because the understanding that you migrate your requests to the latest schema is incomplete. In reality, your goal is to migrate them to some schema that represents the union of all schemas. Latest schema is the best candidate because our business logic is closest to it and API changes are usually additive in nature. But as you see from the aforementioned situation, that's not always the case, which is why sometimes we need another schema: an internal representation of the request which not confined by our API and can have any structure we want. Now let's solve our email problem using an internal schema.

```python
from .versioned_schemas.latest import CreateUserRequest
from cadwyn import internal_representation_of


@internal_representation_of(CreateUserRequest)
class InternalCreateUserRequest(CreateUserRequest):
    email: str | None
```

Now cadwyn will always use `InternalCreateUserRequest` when pushing body field into your business logic instead of `CreateUserRequest`. Note that users will not be able to use any fields from the internal schema and their requests will still be validated by your regular schemas. So even if you added a field `foo` in an internal schema, and your user has passed this field in the body of the request, this field will not get to the internal schema because it will be removed at the moment of validation (or even an error will occur if you use `extra="ignore"`).

I would, however, advise you put it in an unversioned directory and inherit it from your latest schema to minimize the chance of human errors.

## Version changes with side effects

Sometimes you will use API versioning to handle a breaking change in your **business logic**, not in the schemas themselves. In such cases, it is tempting to add a version check and just follow the new business logic such as:

```python
if api_version_var.get() >= date(2022, 11, 11):
    # do new logic here
    ...
```

In cadwyn, this approach is highly discouraged. It is recommended that you avoid side effects like this at any cost because each one makes your core logic harder to understand. But if you cannot, then I urge you to at least abstract away versions and versioning from your business logic which will make your code much easier to read.

To simplify this, cadwyn has a special `VersionChangeWithSideEffects` class. It makes finding dangerous versions that have side effects much easier and provides a nice abstraction for checking whether we are on a version where these side effects have been applied.

As an example, let's use the tutorial section's case with the user and their address. Let's say that we use an external service to check whether user's address is listed in it and return 400 response if it is not. Let's also say that we only added this check in the newest version.

```python
from cadwyn.structure import VersionChangeWithSideEffects


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

## API Version header and context variables

Cadwyn automatically converts your data to a correct version and has "version checks" when dealing with side effects as described in [the section above](#version-changes-with-side-effects). It can only do so using a special [context variable](https://docs.python.org/3/library/contextvars.html) that stores the current API version.

Use `cadwyn.get_cadwyn_dependency` to get a `fastapi.Depends` that automatically sets this contextvar based on a header name that you pick.

You can also set the variable yourself or even pass a different compatible contextvar to your `cadwyn.VersionBundle` constructor.
