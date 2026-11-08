# Endpoint migrations

Note that the `endpoint(...)` constructor contains a second argument that describes the methods of the endpoints you would like to edit. If you have two routes for a single endpoint and you include both of their methods in the instructions, both of them will be changed.

## Defining endpoints that did not exist for new versions

If you have an endpoint in an old version **but want to delete it in a new version**, define it as usual with all your other endpoints but mark it as deleted...

```python
{! ./docs_src/concepts/endpoint_migrations/block001.py [ln:6-8] !}
```

and then define it as existing in one of the older versions:

```python
{! ./docs_src/concepts/endpoint_migrations/block002.py !}
```

## Defining endpoints that did not exist for old versions

If you have an endpoint in a new version that should not exist in older versions, define it as usual and then mark it as "nonexistent" in old versions. Note that this approach is [not recommended for adding new endpoints](../how_to/change_endpoints/index.md#add-a-new-endpoint).

```python
{! ./docs_src/concepts/endpoint_migrations/block003.py !}
```

## Changing endpoint attributes

If you want to change an endpoint attribute (like description) in a new version, you can return its value in all older versions like this:

```python
{! ./docs_src/concepts/endpoint_migrations/block004.py !}
```

However, a migration is required only if it is a breaking change for your users.

### Warning: dependency changes

Note that changing FastAPI endpoint dependencies is only going to affect the initial validation. Cadwyn will run your altered dependencies for each request to the endpoint but ultimately your endpoint code is always going to use the HEAD version of your dependencies. So be careful.

Also note that if some of your dependencies are added at app/router level, they **are** going to be overwritten by the instructions above. Most of the time it is rather safe, however, as all the necessary dependencies will still run on HEAD version.

## WebSocket endpoint migrations

Use `["WEBSOCKET"]` to select a FastAPI WebSocket route defined with `@router.websocket()`. It supports `.didnt_exist`, `.existed`, and `.had(path=..., name=..., dependencies=...)`. Mark sockets that only exist in older versions with `@router.only_exists_in_older_versions`, just like HTTP endpoints.

```python
{! ./docs_src/concepts/endpoint_migrations/block007.py !}
```

In this example, clients of `2024-02-01` connect to `/ws/general` with the `token` header. Clients of `2024-01-01` connect to `/chat/general` with `legacy-token` instead. The older version also keeps `/legacy` and has no `/notifications` socket. Both clients pass their API version in the `x-api-version` header; [path versioning and constant defaults](api_version_parameter.md#websocket-connections) also work.

Unlike HTTP endpoints, WebSockets run only the dependencies of the selected version, including dependency overrides and cleanup. `.had(dependencies=...)` replaces the route's dependency list, including dependencies inherited from included routers. Dependencies declared in the endpoint signature still run. A path change must keep the same path parameter names. Use `func_name` to distinguish sockets with the same path, as in the endpoint duplicates example below.

`WEBSOCKET` must be the only method in its selector, and `.had()` accepts only `path`, `name`, and `dependencies` for sockets. Other attributes and mixed HTTP/WebSocket selectors raise an error when the instruction is defined. Request and response data converters remain HTTP-only.

WebSockets stay out of OpenAPI. Cadwyn does not apply schema or message migrations to WebSocket connections; handle message formats in your endpoint code. These instructions support FastAPI `APIWebSocketRoute` routes only: plain Starlette `WebSocketRoute`s (`@router.websocket_route()`), plain Starlette `Route`s, `Mount`, and static files are outside their scope.

## Dealing with endpoint duplicates

Sometimes, when making advanced changes between versions, you need to rewrite your endpoint function entirely. So essentially you would have the following structure:

```python
{! ./docs_src/concepts/endpoint_migrations/block005.py !}
```

As you can see in the code example above, the two functions have the same parameters and path decorators. And if you have many versions, you may have even more functions like these two. The recommended way to instruct Cadwyn to restore one of them and delete the other is the following:

```python
{! ./docs_src/concepts/endpoint_migrations/block006.py !}
```
Using a more specific `func_name` allows to distinguish between different functions that affect the same routes.
