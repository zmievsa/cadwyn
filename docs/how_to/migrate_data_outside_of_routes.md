# Migrate data outside of routes

`migrate_request()` and `migrate_response()` migrate requests and responses outside of routes, e.g. for webhooks, messages in queues, workers, or an API that isn't built with FastAPI. They run the same migrations by schema as routes, so the version changes that you've written for your routes give the same data either way. [Migrations outside of routes](../concepts/version_changes.md#migrations-outside-of-routes) describes them. This guide is for the code that migrates data outside of routes in other ways.

## From `migrate_response_body()`

`migrate_response_body()` keeps working as before, so you can move at your own pace. Where you have

```python
{! ./docs_src/how_to/migrate_data_outside_of_routes/block001.py !}
```

use

```python
{! ./docs_src/how_to/migrate_data_outside_of_routes/block002.py !}
```

Here's what changes:

* The body argument is `body` instead of `latest_body`, and you get the body from the `body` of the result.
* You can pass objects of your HEAD models, which `migrate_response_body()` rejects. They become data before the migrations run, as in routes. Migrations always get data, so the ones that turn the object into data themselves can stop doing it.
* The migrated body is validated as FastAPI validates responses: by the attributes of objects too, and without your overrides of `model_validate()`.
* The status code and the headers are part of the result, so the headers and the cookies that migrations set are no longer thrown away. Pass `status_code` and `headers` if your migrations need them.
* If a migration turns the response into an error, you get the error body and its status code, while `migrate_response_body()` fails to validate the error body as your model.

## From your own migration loop

If you call the migrations of your version changes yourself, e.g. by walking `VersionBundle.versions` and calling the instructions in `alter_request_by_schema_instructions` and `alter_response_by_schema_instructions`, you can use `migrate_request()` and `migrate_response()` instead. For example, to bring a message of an older version up to HEAD:

```python
{! ./docs_src/how_to/migrate_data_outside_of_routes/block003.py !}
```

They also do what routes do around the migrations, which a loop of your own usually doesn't:

* They validate the body of the older version first, and migrate only the fields that it has set, by their aliases.
* They give the HEAD the members of its own enums instead of the members of the copies of enums that older versions have.
* They restore the validation aliases that the migrated body needs, and, in requests, the `Json[...]` values.
* Once a migration turns a response into an error, they only run the migrations that set `migrate_http_errors=True`.
* They take dates and pick the closest older version, as `migrate_response_body()` does.
* They migrate between any two versions: pass `to_version` to `migrate_request()` or `from_version` to `migrate_response()` if your loop doesn't start or end at HEAD.

Your loop keeps working too: the attributes that it uses are part of the [supported API](../theory/design_decisions.md#supported-api).

## Without FastAPI

Cadwyn still installs FastAPI. A separate package that only needs Pydantic is planned in [#203](https://github.com/zmievsa/cadwyn/issues/203). Until then, `migrate_request()` and `migrate_response()` already work in projects that don't use FastAPI otherwise. Headers need Starlette, which FastAPI installs.
