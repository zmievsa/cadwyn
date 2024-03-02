# Main App

Cadwyn's standard usage is done with a single customized FastAPI app: `cadwyn.Cadwyn`. It accepts all the same arguments as `FastAPI` three more keyword-only arguments:

* Required `versions: VersionBundle` describes [all versions](#versionbundle) within your application
* Required `latest_schemas_package: ModuleType` is your [latest package](./service_structure.md#service-structure) that contains the latest versions of your versioned schemas
* Optional `api_version_header_name: str = "x-api-version"` is the header that Cadwyn will use for [routing](#routing) to different API versions of your app

After you have defined a main app, you can add versioned API routers to it using `Cadwyn.generate_and_include_versioned_routers(*routers)`

```python
from cadwyn import VersionedAPIRouter, Cadwyn
from versions import my_version_bundle


router = VersionedAPIRouter(prefix="/users")


@router.get("/users/", tags=["users"])
async def read_users():
    return [{"username": "Rick"}, {"username": "Morty"}]


@router.get("/users/{username}", tags=["users"])
async def read_user(username: str):
    return {"username": username}


app = Cadwyn(versions=my_version_bundle)
app.generate_and_include_versioned_routers(router)
```

That's it! `generate_and_include_versioned_routers` will generate all versions of your routers based on the `versions` argument and will use schemas from the versioned schema directories parallel to `versions.latest_schema_package`.

## Routing

Cadwyn is built on header-based routing. First, we route requests to the appropriate API version based on the version header (`x-api-version` by default). Then we route by the appropriate url path and method. Currerntly, Cadwyn only works with ISO date-based versions (such as `2022-11-16`). If the user sends an incorrect API version, Cadwyn picks up the closest lower applicable version. For example, `2022-11-16` in request can be matched by `2022-11-15` and `2000-01-01` but cannot be matched by `2022-11-17`.

However, header-based routing is only the standard way to use Cadwyn. If you want to use any other sort of routing, you can use Cadwyn directly through `cadwyn.generate_versioned_routers`. Just remember to update the `VersionBundle.api_version_var` variable each time you route some request to a version. This variable allows Cadwyn to do [side effects](./version_changes.md#version-changes-with-side-effects) and [data migrations](#data-migrations).

### VersionedAPIRouter

Cadwyn has its own API Router class: `cadwyn.VersionedAPIRouter`. You are free to use a regular `fastapi.APIRouter` but `cadwyn.VersionedAPIRouter` has a special decorator `only_exists_in_older_versions(route)` which allows you to define routes that have been previously deleted. First you define the route and than add this decorator to it.
