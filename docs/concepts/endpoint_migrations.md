# Endpoint migrations

Note that the endpoint constructor contains a second argument that describes the methods of the endpoints you would like to edit. If you have two routes for a single endpoint and you put both of their methods into the instruction -- both of them are going to be changed as you would expect.

## Defining endpoints that didn't exist in new versions

If you had an endpoint in old version but do not have it in a new one, you must still define it but mark it as deleted.

```python
@router.only_exists_in_older_versions
@router.get("/users/{user_id}")
async def my_old_endpoint():
    ...
```

and then define it as existing in one of the older versions:

```python
from cadwyn import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint("/users/{user_id}", ["GET"]).existed,
    )
```

## Defining endpoints that didn't exist in old versions

If you have an endpoint in your new version that must not exist in older versions, you define it as usual and then mark it as "non-existing" in old versions:

```python
from cadwyn import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint("/companies/{company_id}", ["GET"]).didnt_exist,
    )
```

## Changing endpoint attributes

If you want to change any attribute of your endpoint in a new version, you can return the attribute's value in all older versions like so:

```python
from cadwyn import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint("/users/{user_id}", ["GET"]).had(
            description="My old description"
        ),
    )
```

### Dependency alteration warning

Note that changing endpoint `dependencies` is only going to affect the initial validation. So Cadwyn will take your altered dependencies and run them on each request to the endpoint but ultimately your endpoint code is always going to use the HEAD version of your dependencies. So be careful.

Note also that if some of your dependencies were added at app/router level -- they **are** going to be overwritten by this instruction. Most of the time it is rather safe, however, as all the necessary dependencies will still run on HEAD version.

## Dealing with endpoint duplicates

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
from cadwyn import VersionChange, endpoint


class UseParamsInsteadOfHeadersForUserNameFiltering(VersionChange):
    description = (
        "Use params instead of headers for user name filtering in GET /users "
        "because using headers is a bad API practice in such scenarios."
    )
    instructions_to_migrate_to_previous_version = (
        # We need to specify the name, otherwise, we will encounter an exception due to having two identical endpoints
        # with the same path and method
        endpoint(
            "/users",
            ["GET"],
            func_name="get_users_by_name_before_we_started_using_params",
        ).existed,
        # We also need to specify the name here because, following the instruction above,
        # we now have two existing endpoints
        endpoint("/users", ["GET"], func_name="get_users_by_name").didnt_exist,
    )
```

So by using a more concrete `func_name`, we are capable to distinguish between different functions that affect the same routes.
