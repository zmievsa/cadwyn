# Main App

Cadwyn's standard usage involves a single customized FastAPI app: `cadwyn.Cadwyn`. It accepts the same arguments as `fastapi.FastAPI` plus two additional keyword-only arguments:

* Required `versions: VersionBundle` describes [all versions](./version_changes.md#versionbundle) of your app
* Optional `api_version_parameter_name: str = "x-api-version"` is the parameter that Cadwyn uses for [routing](#routing) to different API versions of your app

After you have defined the main app, you can add versioned API routers to it using `Cadwyn.generate_and_include_versioned_routers(*routers)`

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

That's it. `generate_and_include_versioned_routers()` will generate all versions of your routers based on the `versions` argument.

## Routing

Cadwyn is built on header-based routing. First, requests are routed to the appropriate API version based on the version header (`x-api-version` by default). Second, requests are routed by the appropriate url path and method. Currently, Cadwyn works only with ISO 8601 date-based versions (such as `2022-11-16`). If a user provides a date that does not match any of the versions exactly, Cadwyn selects the closest earlier applicable version. For example, `2022-11-16` in a request can be matched by `2022-11-15` or `2000-01-01` but not by `2022-11-17`.

However, header-based routing is the default way to use Cadwyn. If you want to use any other form of routing, you can use Cadwyn directly through `cadwyn.generate_versioned_routers()` or subclass `cadwyn.Cadwyn` with a different router and middleware. Remember to update the `VersionBundle.api_version_var` variable each time you route a request to a version. This variable allows Cadwyn to do [side effects](./version_changes.md#version-changes-with-side-effects) and [data migrations](./version_changes.md#data-migrations).

### VersionedAPIRouter

Cadwyn has its own API Router class: `cadwyn.VersionedAPIRouter`. You are free to use a regular `fastapi.APIRouter` but `cadwyn.VersionedAPIRouter` has a special decorator `only_exists_in_older_versions(route)` which allows you to define routes that have been previously deleted. First you define the route and then add this decorator to it.

## Custom docs static assets

By default, Swagger UI and ReDoc load JavaScript and CSS from CDNs. You can override these URLs to serve assets locally:

```python
app = Cadwyn(
    versions=my_version_bundle,
    swagger_js_url="/static/swagger-ui-bundle.js",
    swagger_css_url="/static/swagger-ui.css",
    swagger_favicon_url="/static/favicon.png",
    redoc_js_url="/static/redoc.standalone.js",
    redoc_favicon_url="/static/favicon.png",
)
```
