# Endpoint migrations

Note that the `endpoint(...)` constructor contains a second argument that describes the methods of the endpoints you would like to edit. If you have two routes for a single endpoint and you include both of their methods in the instructions, both of them will be changed.

## Defining endpoints that did not exist for new versions

If you have an endpoint in an old version **but want to delete it in a new version**, define it as usual with all your other endpoints but mark it as deleted...

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

## Defining endpoints that did not exist for old versions

If you have an endpoint in a new version that should not exist in older versions, define it as usual and then mark it as "nonexistent" in old versions. Note that this approach is [not recommended for adding new endpoints](../how_to/change_endpoints/index.md#add-a-new-endpoint).

```python
from cadwyn import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint("/companies/{company_id}", ["GET"]).didnt_exist,
    )
```

## Changing endpoint attributes

If you want to change an endpoint attribute (like description) in a new version, you can return its value in all older versions like this:

```python
from cadwyn import VersionChange, endpoint


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        endpoint("/users/{user_id}", ["GET"]).had(
            description="My old description",
        ),
    )
```

However, a migration is required only if it is a breaking change for your users.

### Warning: dependency changes

Note that changing FastAPI endpoint dependencies is only going to affect the initial validation. Cadwyn will run your altered dependencies for each request to the endpoint but ultimately your endpoint code is always going to use the HEAD version of your dependencies. So be careful.

Also note that if some of your dependencies are added at app/router level, they **are** going to be overwritten by the instructions above. Most of the time it is rather safe, however, as all the necessary dependencies will still run on HEAD version.

## Dealing with endpoint duplicates

Sometimes, when making advanced changes between versions, you need to rewrite your endpoint function entirely. So essentially you would have the following structure:

```python
from typing import Annotated

from cadwyn import VersionedAPIRouter
from fastapi.headers import Header
from fastapi.params import Param

router = VersionedAPIRouter()


@router.only_exists_in_older_versions
@router.get("/users")
def get_users_by_name_before_starting_to_use_params(
    user_name: Annotated[str, Header()],
):
    """Some code that references user_name"""


@router.get("/users")
def get_users_by_name(user_name: Annotated[str, Param()]):
    """Some code that references user_name"""
```

As you can see in the code example above, the two functions have the same parameters and path decorators. And if you have many versions, you may have even more functions like these two. The recommended way to instruct Cadwyn to restore one of them and delete the other is the following:

```python
from cadwyn import VersionChange, endpoint


class UseParamsInsteadOfHeadersForUserNameFiltering(VersionChange):
    description = (
        "Use params instead of headers for user name filtering in 'GET /users' "
        "because using headers is a poor API practice in such scenarios."
    )
    instructions_to_migrate_to_previous_version = (
        # Specify the name; otherwise you will encounter an exception due to
        # having two identical endpoints with the same parameters and path decorators
        endpoint(
            "/users",
            ["GET"],
            func_name="get_users_by_name_before_starting_to_use_params",
        ).existed,
        # Also specify the name here because, following the instructions above,
        # we now have two existing endpoints
        endpoint("/users", ["GET"], func_name="get_users_by_name").didnt_exist,
    )
```
Using a more specific `func_name` allows to distinguish between different functions that affect the same routes.
