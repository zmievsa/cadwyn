# Changelog

All notable changes to this project will be documented in this file.
Please follow [the Keep a Changelog standard](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed

- Parametrized generic models such as `Page[Item]` now use the versioned copies of their type args when the args refer to models or enums that have schema or enum instructions, including the ones in `HeadVersion`. So older versions validate and serialize these args with their own models instead of the latest ones, and clients that relied on latest-shaped models inside generics may need converters. Changes from `HeadVersion` now also apply to these args in every version, including the latest one. The OpenAPI names of these generics can change in every version because their quoted type args are now evaluated, e.g. `Page[list["Item"]]` is named `Page_list_Item__` instead of `Page_list_str__`. Generics whose type args do not refer to such models are not affected.
- Models that refer to themselves, directly or through other models (e.g. `children: "list[Category]"`), are no longer copied for every version if they have no schema instructions in any version, including `HeadVersion`, do not refer to models or enums with instructions, and are not parents of models that need versioned copies. Every version uses the original model itself, so `generate_versioned_models(versions)[version][Category] is Category`. All other models are copied as before. Models defined after Cadwyn has generated the versioned models, e.g. a subclass of a self-referencing model with a field of a changed model, do not change which self-referencing models are reused.
- Parametrized generic models such as `Page[Item]` whose type args refer to self-referencing models (e.g. `Item.category: Category`) now use the versioned copies of their type args in older versions when the args also refer to models or enums with instructions, like other generic models. Before, the self-reference made them keep the latest type args. So older versions now validate and serialize such args with their old models: e.g. if `Item.color` is an enum that `enum(Color).didnt_have("blue")` changes, older versions now reject `"blue"` in request bodies with a 422 where they used to accept it, and responses that contain `"blue"` fail in older versions. Add migrations if your older clients rely on the values that their version did not have.
- `migrate_response_body` no longer accepts instances of the latest models inside generic models whose type args are versioned, e.g. `Page[Item](...)` or `{"items": [Item(...)]}` for `Page[Item]`, and raises a `ValidationError` for them in every version, including the latest one, as it already did for other versioned models nested in the response model. Pass plain data such as the result of `model_dump()` instead.

### Fixed

- Fixed every request and the OpenAPI of an app failing with a 500 (`RecursionError`, reported as an `ImportError`) when a route uses a model that refers to itself, directly or through other models, such as `children: "list[Category]"`, even with a single version. This is fixed for such models that have no instructions, do not refer to models with instructions, and are not parents of such models; the other ones still fail (#419).
- Fixed older API versions adding `"example": {}` to the OpenAPI of path, query, header, cookie, and body parameters declared through default values such as `= Query()` (#417).
- Restored type checking compatibility with Pydantic 2.14 and ty 0.0.86.
- Fixed older API versions using current models inside parametrized generic models such as `Page[Item]` in requests, responses, and OpenAPI (#416).
- Fixed a `TypeError` ("cannot be parametrized because it does not inherit from typing.Generic") in older API versions for generic models whose fields parametrize generic models with their own type parameters, such as `items: list[Wrapper[T]]` or `next: Optional["LinkedPage[T]"]`.
- Fixed older API versions failing to start when a model uses `typing.Self` in its annotations.
- Fixed older API versions failing to start when a model uses annotations that cannot be rebuilt from their arguments, such as a bare `typing.Callable` or `Unpack[Ts]`, or a default factory without an inspectable signature, such as `functools.partial(dict, count=1)`.
- Fixed older API versions failing on Python 3.14 when versioned models or their type aliases use names that are only imported under `TYPE_CHECKING`, e.g. in `ClassVar` annotations.
- Fixed enums that have no enum instructions in any version being copied for every version. Their members were not the members of the original enum, so strict models rejected them in responses (a 500), and request bodies with such enums failed with `CadwynHeadRequestValidationError` in every version as soon as any model had instructions.
- Fixed `TypeError: unhashable type` on older API versions when an endpoint's request body or response model is an unhashable annotation such as `list[Annotated[int, {"description": "item"}]]`.
- Fixed `functools.partial` dependencies losing the annotations of their remaining parameters, which turned their sub-dependencies and request bodies into required query parameters. Partials of async functions also returned unawaited coroutines.
- Fixed dependencies whose parameters are only described by `__signature__` losing their parameter types, which skipped type conversion, crashed constraints such as `ge` with a 500, and removed the types from OpenAPI.
- Fixed async generator dependencies not receiving exceptions raised after their `yield`, which skipped their `except` blocks (e.g. a database rollback) and delayed their cleanup until garbage collection.
- Fixed versioned dependencies losing the `scope` of `Depends` and `Security`.
- Fixed `app.dependency_overrides` getting ignored in older API versions for dependencies that are class instances, bound methods, or partials. Older versions used copies of such dependencies, which also hid changes to their state.
- Fixed parameter defaults such as `filters: Filters = Query()` in endpoints and dependencies being shared between API versions and between apps built from the same router. FastAPI changes them while building routes, so their OpenAPI and validation could use the models of another version or app.
- Fixed decorated dependencies, such as `functools.lru_cache` ones, failing in older API versions when their annotations are postponed (`from __future__ import annotations`).
- Fixed a `NameError` at startup on Python 3.14 when a dependency's annotations use a name that only exists for type checkers (e.g. one imported under `if TYPE_CHECKING:`).
- Fixed router generation failing for dependencies whose return annotation only type checkers can evaluate, such as `-> "pd.Series[int]"` with postponed annotations, or whose returned model has a default factory without a signature, such as `time.time` before Python 3.13. Like FastAPI, Cadwyn no longer evaluates or versions the return annotations of dependencies.
- Fixed `Annotated` dependencies whose type is not versioned, such as `Annotated[list[int], Depends(get_values)]`, using the dependency of the head or another API version. This made versioned request constraints, such as a `min_length` or a removed enum member, get ignored in both validation and OpenAPI.
- Fixed the overridden `__call__` of `functools.partial` subclass dependencies being skipped on Python 3.13+ when the subclass forwards attribute lookups to the wrapped function (e.g. through `__getattr__`) or defines its own `__dict__` property.
- Fixed `schema(...).field(...).had(...)` being silently ignored in older API versions when it changed a constraint the field already had, such as `min_length`, `max_length`, `ge`, `le`, or `pattern`, declared through `Annotated[..., Field(...)]`, or through `Field(...)` under `from __future__ import annotations`.
- Fixed `schema(...).field(...).didnt_have(...)` having no effect in older API versions on attributes declared through `Annotated[..., Field(...)]`, such as `min_length` or `description`.
- Fixed `schema(...).field(...).didnt_have("title")` failing with `TypeError` when the field's type is `Annotated`, for example because it has an `AfterValidator` or a documentation string.
- Fixed `schema(...).field(...).didnt_have("min_length")` failing with `TypeError` when the field's type has `annotated_types.Len`.
- Fixed `schema(...).field(...).had(strict=...)` and `.had(allow_inf_nan=...)` being silently ignored in older API versions, and `.didnt_have(...)` failing with `TypeError`, when the field's type uses the `Strict` or `AllowInfNan` class itself, such as `Annotated[int, Strict]`.
- Fixed request body fields getting silently replaced by their defaults, or failing as missing, in every API version except head (including the latest one) when the field validates a different plain key than it serializes to, such as `Field(alias="x", validation_alias="v")`, `Field(validation_alias="v")`, `Field(serialization_alias="s")`, `AliasChoices` with a plain key, an `AliasGenerator` with different aliases, or `validate_by_alias=False`. This also covers such fields in nested models, in lists and dicts of models, in `RootModel` bodies, and in form models. Request migrations still see the same keys as before. Values that were dropped before now reach the validation of the head model, so they can fail there like the values of any other field. Fields are left as before when Cadwyn can't be sure their dumped value is valid input, e.g. with custom serializers, `Json`, "before", "wrap" or "plain" validators (also on the body parameter), a custom `__init__` or `model_dump()`, computed fields that share keys with fields, `AliasPath`-only aliases, `extra="allow"`, or keys shared by several fields.
- Fixed requests failing with `CadwynHeadRequestValidationError` (a 500), or optional bodies silently arriving as `None`, in every API version except head, including the latest one, when the request body is embedded (`Body(embed=True)`, a single non-model `Form()` field, or a body parameter next to the body parameter of a dependency) and the endpoint has a parameter named `body`, such as `body: Optional[Item] = Body(None, embed=True)`, or when a single body parameter has an alias that is the name of another non-dependency parameter, such as `item: Item = Body(alias="filter")` next to `filter: str = Query()`. Request migrations of such endpoints now see the request body as the client sent it, like the ones of endpoints whose parameters have other names. File uploads keep failing as before.
- Fixed uploaded files declared as `bytes` in endpoint parameters silently arriving empty (`b""`, with a 200) in every API version except head, including the latest one, e.g. `upload: bytes = File()`, `uploads: list[bytes] = File()`, or `upload: bytes = File()` next to `name: str = Form()`. FastAPI had already read these files for the versioned route, and Cadwyn passed them on to the head route as they were. Cadwyn now passes such files to the head route from their start, unless a request migration replaced, moved, closed, or swapped the stream of them. So dependencies that read the same upload, e.g. as an `UploadFile`, now see its contents in these versions too, as in FastAPI itself. Note that these `bytes` uploads are now held in memory twice per request in these versions.
- Fixed uploaded files declared as `bytes` in the parameters of dependencies, including nested ones, silently arriving empty (`b""`, with a 200) in every API version except head, including the latest one, e.g. `async def get_data(data: bytes = File())` used as `Depends(get_data)`. They now arrive like the ones in endpoint parameters, as in FastAPI itself: when several params read the same file, the first one gets its contents. Overridden dependencies and files that a dependency moved, replaced, or closed after they were read keep working as before. In API versions that add dependencies to an endpoint (`endpoint(...).had(dependencies=...)`), the `bytes` files of dependencies keep working as before too, i.e. they still arrive empty, because the added dependencies don't run again for the head and might have changed the files. A dependency that moves an uploaded file only when `current_dependency_solver` is `"fastapi"` (e.g. to skip a header) can now have the file moved back to its start if FastAPI has read it to its end for a `bytes` param. Move it in the run that gives the endpoint its arguments instead, as the docs on dependency re-execution now describe. The docs also describe `RequestInfo.form` and that request migrations reading uploaded files must seek them back.

## [7.4.1]

### Fixed

- Fixed older API versions using current request models when endpoints use postponed annotations, a regression introduced in 6.0.1.

## [7.4.0]

### Fixed

- Preserve SQLAlchemy and SQLModel classes during API version generation, fixing startup failures for annotated ORM dependencies. Reject migrations targeting these classes with guidance to use separate Pydantic models, and document this restriction in the schema migrations guide (#410).

## [7.3.0]

### Added

- Added native View Transition API animations to documentation navigation, with reduced-motion and unsupported-browser fallbacks.

### Changed

- Updated Starlette documentation links to the project's current official domain.

### Fixed

- Restored compatibility with ty 0.0.78 while preserving FastAPI's coroutine detection on every supported Python version.
- Preserved the `classmethod` binding of `@field_serializer` decorators during model generation. Previously, such serializers were re-registered as instance methods, shifting their arguments by one and raising `PydanticSerializationError` on every serialization.

## [7.2.0]

### Deprecated

- Deprecated changelog generation, including the default `GET /changelog` endpoint, `Cadwyn.generate_changelog()`, `cadwyn.hidden()`, and the `changelog_url` and `include_changelog_url_in_schema` constructor arguments. The feature remains available for backwards compatibility.

## [7.1.2]

### Changed

- Simplified contributor setup and development commands (#387)
- Standardized test names around the `what`, `when`, and `expected` convention and documented test organization (#397)
- Expanded Ruff coverage, including return annotations, preview correctness checks, and explicit tracking of reviewed rules

### Fixed

- Restored compatibility with FastAPI 0.140.2+ after it stopped retaining flattened dependency trees

## [7.1.1]

### Changed

- Migrated static type checking from Pyright to ty

### Fixed

- Return the matched API version in the response header after waterfalling, by @Sanjays2402 (#384)

## [7.1.0]

### Added

- Support FastAPI 0.137.1+ included-router trees, including routes added to included routers after inclusion, for Cadwyn's versioned and unversioned routing

## [7.0.0]

- Breaking change if you relied on lifespan running multiple times: Fixed duplicated lifecycle callbacks in Cadwyn routers, see [this issue](https://github.com/zmievsa/cadwyn/issues/372) for more details
- Fixed `Cadwyn.mount()` routes, including `StaticFiles`, returning 404 instead of being served as unversioned routes (#374)

## [6.2.2]

## [6.2.1]

### Fixed

- Fixed `openapi_tags` to only include tags used by served routes (#368)
- Fixed `response_model` not being respected on routes (#365)

## [6.2.0]

### Added

- Added `swagger_js_url`, `swagger_css_url`, `swagger_favicon_url`, `redoc_js_url`, and `redoc_favicon_url` parameters to `Cadwyn` for customizing docs static asset URLs (#361)

## [6.1.1]

### Fixed

- Fixed compatibility with Starlette 1.0.0 which removed the deprecated `TemplateResponse(name, context)` calling convention

## [6.1.0]

### Added

- Added `form` property to `RequestInfo` for reading and modifying multipart/form-encoded fields (strings and file uploads) inside `convert_request_to_next_version_for` handlers (#359)

### Fixed

- Fixed false `RouterPathParamsModifiedError` when renaming path-versioned endpoints (#357)

## [6.0.4]

### Added

- Python 3.14 support (#345)

### Fixed

- Fixed missing type imports with future annotations in `Depend` leading to `RecursionError` in `generate_versioned_routers` by @Shigerman (#329)
- Resolved `'__getitem__' requires a 'typing.Union'` error by @ulgens (#336)
- Adopted Python 3.14 related union handling changes by @ulgens (#337)
- Adopted Python 3.14 related `http.cookies` changes by @ulgens (#338)

### Removed

- Python 3.9 support by @ulgens (#342)

## [6.0.3]

### Fixed

- Tolerate FastAPI 0.128+ utils removal, restrict fastapi >= 0.128.6 by @shahar1 (#327)

## [6.0.2]

### Fixed

- Resolved `...has no attribute '__annotations__'` error by @ulgens (#334)

## [6.0.1]

### Fixed

- Fixed AttributeError when using callable class dependencies with `from __future__ import annotations`. Note that this introduces another problem in python 3.9 in the same case that is currently not yet solved (#321)

## [6.0.0]

### Fixed

- Support for FastAPI>=0.128.0+

### Removed

- Support for FastAPI < 0.128.0 and Pydantic v1

## [5.6.2]

### Fixed

- Support for FastAPI 0.126.0+ where `_prepare_response_content` may return Pydantic model instances instead of dicts, breaking response migrations (#314)

## [5.6.1]

### Fixed

- Support for FastAPI 0.121.0

## [5.6.0]

### Added

- `schema(...).had(json_schema_extra=...)` support by @csemanish12

## [5.5.0]

- Fix the rest of the issues from fastapi==0.119.0

## [5.4.5]

### Fixed

- Many issues after GenerateJsonSchema getting removed in fastapi==0.119.0

### Changed

- A lot of documentation improvements by @Shigerman

## [5.4.4]

### Fixed

- Fixed KeyError when versioning Pydantic models containing ClassVar annotations

## [5.4.3]

### Fixed

- XSS vulnerability in `/docs` and `/redoc` endpoints where the `version` parameter was not properly sanitized, allowing potential cross-site scripting attacks

## [5.4.2]

### Fixed

- Added support for computed fields

## [5.4.1]

### Fixed

- Fixed import error in python 3.9 when using typing_extensions==3.14.0

## [5.4.0]

### Added

- `typing_inspection` dependency from pydantic team for complex isinstance checks that must be the same between `typing` and `typing_extensions`
- Support for `pydantic>=2.12.0` FieldInfo refactoring from <https://github.com/pydantic/pydantic/pull/11898>

## [5.3.3]

### Fixed

- Fixed pypi badge link in README

## [5.3.2]

### Fixed

- Add support for `pydantic_settings.BaseSettings` in dependencies

## [5.3.1]

### Fixed

- Fix invalid content-length header with custom exception handlers (#273)

## [5.3.0]

### Added

- `api_version_title` and `api_version_description` arguments to `Cadwyn` to allow customizing the API version parameter in the OpenAPI schema

## [5.2.2]

### Fixed

- Whenever a route was migrated by path, we used the path and methods of the request. So if the request wanted "/v1/webhook_settings" and we renamed them to "/v1/webhook_subscriptions" -- all migrations for "/v1/webhook_subscriptions" would not get applied to any requests that wanted "/v1/webhook_settings". This effectively means that previously route renamings were incompatible with by path converters. Now we use the head route id instead of the path and methods of the request for matching so we always know which migrations to apply.

## [5.2.1]

### Fixed

- [#268](https://github.com/zmievsa/cadwyn/issues/268) A bug where we received 404 for all unversioned routes when a default api version was passed to Cadwyn at initialization

## [5.2.0]

### Removed

- `issubclass` dependency

## [5.1.4]

### Fixed

- a bug where a custom HTTP exception was not being copied correctly so its unique properties are ignored

## [5.1.3]

### Fixed

- `__doc__` attribute is now copied from the original enum to the generated enum
- Python 3.12 type aliases and their typing_extensions backports are now supported (including `pydantic.JsonValue` and other `typing_extensions.TypeAliasType` instances)
- The bug when solve_dependencies error on the migration of a request to the latest version responds with a non-json serializable error and cadwyn showed a failed to serialize error instead of the actual error
- Updated minimum FastAPI version to 0.112.4 because embed_body_fields was added in 0.112.4

## [5.1.2]

### Fixed

- Generators not being called when FastAPI validates the initial request

### Added

- `cadwyn.current_dependency_solver` function to check whether cadwyn or FastAPI is currently solving dependencies. It can be used as a dependency for versioned endpoints like so: `current_dependency_solver: Annotated[Literal["fastapi", "cadwyn"], Depends(current_dependency_solver)]`. If your dependency has side effects, you would likely want to only run it once per request. If you want to run it once we migrated the request to the latest version, you should only run it when `current_dependency_solver` is `"cadwyn"`. If you want your dependency to run at the very beginning of handling the request, you should only run it when `current_dependency_solver` is `"fastapi"`.

## [5.1.1]

### Fixed

- Support for `WithJsonSchema` in schema generation

## [5.1.0]

### Added

- Support for python 3.9

## [5.0.0]

### Added

- Support for URL path version prefixes instead of version headers. You can control it with the `api_version_location` argument of `cadwyn.Cadwyn`.
- Support for arbitrary strings as versions. You can control the format of the version with the `api_version_format` argument of `cadwyn.Cadwyn`.
- Extensibility of version picking logic with a new `VersionPickingMiddleware` class that you can pass to the `versioning_middleware_class` argument of `cadwyn.Cadwyn`.
- `api_version_default_value` argument to `cadwyn.Cadwyn` to set a default version for unversioned requests. It can be a string or an async callable that returns a string.

### Changed

- `cadwyn.Version`, `cadwyn.VersionBundle`, and `cadwyn.VersionBundle.api_version_var` now store versions as strings instead of dates. Date types can still be passed to `cadwyn.Version` but there is no guarantee that they will be supported in the future. However, ISO dates with a string type are guaranteed to be supported.
- Cadwyn's `api_version_header_name` argument is now deprecated in favor of `api_version_parameter_name`
- `cadwyn.Cadwyn.add_header_versioned_routers` method is now deprecated in favor of `cadwyn.Cadwyn.generate_and_include_versioned_routers`. It will be removed in version 6.0.0

## Removed

- `HeaderVersioningMiddleware` in favor of `VersionPickingMiddleware` because we now support more than just headers

## [4.6.0]

### Added

- Support for more field attributes in `schema.had()` and `schema.didnt_have()`: `field_title_generator`, `fail_fast`, `coerce_numbers_to_str`, `union_mode`, `allow_mutation`, `pattern`, `discriminator`
- Support for forwardrefs in body fields (for example, when you use `from __future__ import annotations` in the file with your routes)
- Support for forwardrefs in route dependencies

## [4.5.0]

### Added

- `check_usage` argument to request/response by schema converters. Cadwyn always checks whether a schema mentioned in a converter applies to one or more endpoints to guarantee that the converter will apply to at least one endpoint. Sometimes, however, you do not need this validation. For example, when you use these converters for converting webhook bodies. Setting `check_usage=False` makes it possible to skip the validation

## [4.4.5]

### Fixed

- Fix invalid migration of pydantic v1 style root validators when pydantic erases information about their "skip_on_failure" attribute

## [4.4.4]

### Fixed

- Type hints for newest pydantic versions

## [4.4.3]

### Changed

- Bumped package versions

## [4.4.2]

### Fixed

- Non-function (object instances) dependencies not supporting dependency overrides in testing

## [4.4.1]

### Added

- Python 3.13 to CI

## [4.4.0]

### Added

- Support for [webhooks](https://fastapi.tiangolo.com/advanced/openapi-webhooks/) in swagger
- Automatic generation of versioned routes and webhooks upon the first request to Cadwyn. Notice that if you were using some of cadwyn's internal interfaces, this might break your code. If it did, make an issue and let's discuss your use case

## [4.3.1]

### Fixed

- Removed typer from main dependencies

## [4.3.0]

### Changed

- Migrated from poetry to uv (Contributed by @bastienpo)
- Removed CLI and Uvicorn dependencies from Cadwyn installations by default. Added a `standard` extras group to mirror FastAPI (Contributed by @bastienpo)

## [4.2.4]

### Fixed

- Background tasks not functioning in versioned endpoints

## [4.2.3]

### Fixed

- Added support for `embed_body_fields` in `solve_dependencies` and `create_model_field` in FastAPI. FastAPI has made a breaking change for these interfaces which is why we had to fix it
- Fixed invalid imports in quickstart docs
- Fixed default dependencies not including the CLI for FastAPI, thus causing the quickstart docs to be invalid

## [4.2.2]

### Fixed

- FastAPI Servers list did not include root path when mounted as a sub-app unless specified directly within the user defined server list (Contributed by @OD-tpeko)
- OpenAPI spec did not include the `summary` field (Contributed by @OD-tpeko)

## [4.2.1]

### Fixed

- Previously the docs were showing wrong versioned doc paths when cadwyn was mounted as a sub-app (Contributed by @OD-tpeko)

## [4.2.0]

### Added

- Automatic changelog generation from version changes using `Cadwyn.generate_changelog` method and `GET /changelog` endpoint.
- Automatic creation of versioned_routers based on the `VersionBundle` passed to `Cadwyn` which means that all versions mentioned in the version bundle will already be available for routing even without the use of `generate_and_include_versioned_routers`

## Changed

- Renamed `Version.version_changes` to `Version.changes`

### Removed

- `regex`, `include`, `min_items`, `max_items`, and `unique_items` arguments were removed from `schema(...).field(...).had`. Notice that it's not a breaking change for most cases because passing these arguments caused exceptions

## [4.1.0]

### Added

- Exposed `cadwyn.generate_versioned_models` that allow you to generate pydantic models and enums even without a FastAPI app

## [4.0.0]

Versions 3.x.x are still supported in terms of bug and security fixes but all the new features will go into versions 4.x.x.

### Added

- Runtime schema/enum generation
- Support for versions as ISO date strings instead of dates in `cadwyn.migrate_response_body` and `cadwyn.Version`

### Removed

- Pydantic 1 support
- Code generation from everywhere. It is now completely replaced by runtime generation (so schemas/enums are generated in the same manner as endpoints). This allows Cadwyn to version things outside of your project and allows you to pick any project structure unlike codegen that required a single "head" directory with all the versioned modules.
- CLI commands for codegen
- `cadwyn.main` because it is replaced by `cadwyn.__init__`
- `cadwyn.structure.module` as it was only necessary in codegen
- `cadwyn.VersionBundle.latest_schemas_package` and `cadwyn.VersionBundle.head_schemas_package`, `cadwyn.VersionBundle.versioned_modules`, `cadwyn.VersionBundle.versioned_directories_with_head`, `cadwyn.VersionBundle.versioned_directories_without_head`, because they were only necessary in code generation
- `cadwyn.Cadwyn.add_unversioned_routers` as you can now simply use FastAPI's `include_router`
- `cadwyn.Cadwyn.add_unversioned_routes` as you can now simply use any of FastAPI's methods for adding routes directly to the app
- `cadwyn.Cadwyn.enrich_swagger` as its functionality has been automated
- `cadwyn.InternalRepresentationOf` as it was deprecated previously and is now replaced with HeadVersion migrations

### Changed

- `VersionBundle.migrate_response_body` is no longer a method of `VersionBundle` and is now importable directly from `cadwyn` as a function
- `cadwyn.structure` is no longer recommended to be used directly because everything from it is now available in `cadwyn` directly

## [3.15.8]

### Fixed

- Invalid unparseable JSON response without quotes when the response was a raw string JSONResponse
- An exception raised during codegen if a pydantic model or its parent were created within some indent such as classes defined under if statements

## [3.15.7]

### Fixed

- Wrong globals being used for wrapped endpoints in older versions, sometimes causing FastAPI to fail to resolve forward references on endpoint generation (see #192 for more details)
- dependency_overrides not working for old versions of the endpoints because they were wrapped and wraps did not have the same `__hash__` and `__eq__` as the original dependencies

## [3.15.6]

### Added

- `HeadVersion` and `Version` into `cadwyn.__init__` so now they are directly importable from `cadwyn`

### Fixed

- Fix dependencies from other libraries not resolving if they use fastapi.Request or fastapi.Response (added svcs-specific test)
- Now a proper exception is used when no dated version was passed into `VersionBundle`

## [3.15.5]

### Fixed

- Fix dependency overrides not working for versioned routes

## [3.15.4]

### Changed

- `Cadwyn.enrich_swagger` is now completely unnecessary: OpenAPI is now generated at runtime. It also now does not do anything, is deprecated, and will be removed in a future version

### Fixed

- fastapi-pagination now does not require an explicit call to `Cadwyn.enrich_swagger`

## [3.15.3]

### Changed

- `Cadwyn.router.routes` now includes all existing routers within the application instead of just unversioned routes

### Fixed

- Compatibility with fastapi-pagination
- High cardinality of metrics for routers with path variables in starlette-exporter

## [3.15.2]

### Fixed

- OpenAPI not being generated when lifespan was used

## [3.15.1]

### Fixed

- Badge links in the readme

## [3.15.0]

### Changed

- Optimized the calls to enrich_swagger which now happen on the startup event, once for the whole application

### Fixed

- Oauth2 authentication parameters did not get passed to swagger

## [3.14.0]

### Added

- Current API version to per-version openapi.json

## [3.13.0]

### Added

- Validation for path converters to make sure that impossible HTTP methods cannot be used
- Validation for both path and schema converters to make sure that they are used at some point. Otherwise, router generation will raise an error

## [3.12.1]

### Fixed

- `fastapi.Response` subclasses with non-null bodies and 500 response causing the response to not get returned
- `fastapi.Response` subclasses had invalid content length if migration affected it

## [3.12.0]

### Changed

- Rewritten header routing logic and structure to support the full feature set of FastAPI

## [3.11.1]

### Fixed

- Modules and enums from head versions not being detected and thus causing errors

## [3.11.0]

### Changed

- Header router is no longer reliant on the API version header -- now it simply takes the API version from the `VersionBundle.api_version_var`, thus making it easy for someone to extend header routing and set their own rules for how the default version is chosen

## [3.10.1]

### Fixed

- Previous version introduced a minor breaking change: if any old users depended on the pure `generate_versioned_routers` interface, their work would receive a minor yet simple breaking change.

## [3.10.0]

**Yanked** due to a minor breaking change that we fixed in 3.10.1.

### Added

- The new approach to internal schemas: instead of having them duplicate certain fields from `latest`, we introduced a new `HEAD` version -- the only one the user maintains by hand. All requests get migrated to `HEAD` and latest schemas are generated from `HEAD`. `cadwyn.structure.HeadVersion` was added to give us the ability to have migrations between `HEAD` and latest, thus eliminating the need for `InternalRepresentationOf` because all the used schemas are now the internal representations

### Changed

- `latest` is now named `head` because it no longer represents the newest version. Instead, it is the the `internally used` version and the version that is used for generating all other versions.
- the newest version is not aliased from `latest` anymore. Instead, it is generated like all the rest
- deprecated `InternalRepresentationOf` and the concept of `internal schemas` in favor of `HeadVersion` migrations

## [3.9.1]

### Fixed

- A broken link to docs in README.md

## [3.9.0]

### Added

- Support for getting openapi.json routes using API version headers instead of path query params

## [3.8.0]

### Added

- Discord status badge in README
- Logos to existing status badges in README
- An ability to specify multiple schemas when using `convert_request_to_next_version_for` and `convert_response_to_next_version_for` to be able to migrate multiple types of schemas using the same converter
- Redoc support

### Removed

- Dependency from verselect. Now it is included as a part of Cadwyn

### Fixed

- `h11._util.LocalProtocolError` when raising `HTTPException(status_code=500)`

## [3.7.1]

### Fixed

- Error message for changing path params of an endpoint in an incompatible manner which listed methods instead of path params

### Changed

- Deprecated `cadwyn generate-code-for-versioned-packages` and added `cadwyn codegen` instead. It doesn't require `template_package` argument anymore and does not have the `ignore_coverage_for_latest_aliases` argument as we plan to remove this feature in the future. So it only requires `version_bundle`.

## [3.7.0]

### Changed

- Deprecated `cadwyn generate-code-for-versioned-packages` and added `cadwyn codegen` instead. It doesn't require `template_package` argument anymore and does not have the `ignore_coverage_for_latest_aliases` argument as we plan to remove this feature in the future. So it only requires `version_bundle`.

## [3.6.6]

### Fixed

- When a class-based dependency from **fastapi** was used (anything security related), FastAPI had hardcoded `isinstance` checks for it which it used to enrich swagger with functionality. But when the dependencies were wrapped into our function wrappers, these checks stopped passing, thus breaking this functionality in swagger. Now we ignore all dependencies that FastAPI creates. This also introduces a hard-to-solve bug: if FastAPI's class-based security dependency was subclassed and then `__call__` was overridden with new dependencies that are versioned -- we will not migrate them from version to version. I hope this is an extremely rare use case though. In fact, such use case breaks Liskov Substitution Principle and doesn't make much sense because security classes already include `request` parameter which means that no extra dependencies or parameters are necessary.

## [3.6.5]

### Fixed

- When a class-based dependency was used, its dependant was incorrectly generated, causing all affected endpoints to completely stop functioning

## [3.6.4] <!-- Test release -->

## [3.6.3]

### Fixed

- A rare pydantic 2 bug that caused `BaseModel` annotations to be corrupted when new fields were added to the schema

## [3.6.2]

### Fixed

- Removed exception when creating `cadwyn.Cadwyn` without `latest_schemas_package` as it was a minor breaking change

## [3.6.0]

### Added

- Add `cadwyn.VersionBundle.migrate_response_body` that allows us to migrate response bodies outside of routing and FastAPI
- `latest_schemas_package` argument to `cadwyn.VersionBundle` to support the migration above

### Removed

### Changed

- We now raise a 5xx error (`cadwyn.exceptions.CadwynLatestRequestValidationError`) whenever a request migration caused our payload to be incompatible with latest request schemas
- Deprecated `cadwyn.main` and use `cadwyn.applications` instead
- Deprecated `latest_schemas_package` argument in `cadwyn.Cadwyn`

## [3.5.0]

### Fixed

- Previously, Cadwyn did not set the default status code for ResponseInfo

### Added

- HTTP status error handling in response converters using `convert_response_to_previous_version_for(...,  migrate_http_errors=True)`

## [3.4.4]

### Fixed

- Request and response converters were not applied when path params were present

## [3.4.3]

### Added

- `RouterPathParamsModifiedError` is now raised if `endpoint(...).had(path=...)` has different path params than the original route

## [3.4.2]

### Fixed

- Fix import aliases in nested `__init__.py` files generating incorrectly for latest version

## [3.4.1]

### Fixed

- If the endpoint specified a single non-pydantic (list/dict) body parameter, Cadwyn failed to serialize the body

## [3.4.0]

### Added

- `schema(...).validator(...).existed` and `schema(...).validator(...).didnt_exist` instructions for simplistic manipulation of validators
- Automatic deletion of validators when the fields they validate get deleted
- `schema(...).field(...).didnt_have` for unsetting field attributes
- Improved support for `typing.Annotated` in schemas
- Full preservation of original abstract syntax trees for all field values and annotations

### Fixed

- If the user wrote a wrong signature in a transformer decorated by `convert_request_to_next_version_for` or `convert_response_to_previous_version_for`, the text of the error suggested the wrong argument count and names

## [3.3.4]

### Fixed

- Added backwards compatibility for FastAPI < 0.106.0

## [3.3.3]

### Fixed

- Guaranteed that it is impossible to release cadwyn with the wrong pydantic dependency

## [3.3.2]

### Fixed

- Downgrade required version of verselect for backwards compatibility

## [3.3.1]

### Fixed

- Removed lazy migrations as they were producing incorrect results when there were no migrations but when there were schema changes
- Added compatibility with fastapi>=0.109.0

## [3.3.0]

### Fixed

- If a user used a FastAPI/Starlette `StreamingResponse` or `FileResponse`, we still tried to access its `body` attribute which caused an `AttributeError`

## [3.2.0]

### Added

- Sponsors section to README and docs, along with Monite as our main and only current sponsor ✨

## [3.1.3]

### Fixed

- Switched to `better-ast-comments` because `ast-comments` had no license listed on pypi (even though its actual license was MIT) which caused some dependency checking tools to report it as unlicensed

## [3.1.2]

### Changed

- Migrate from black to ruff-format

### Fixed

- A rare Pydantic 2 bug in internal body schema handling when it was applied too early, causing partially incomplete data to arrive to the handler

## [3.1.1]

### Fixed

- Previously we did not pass `dependency_overrides_provider`, `response_model_exclude_unset` `response_model_exclude_defaults`, and `response_model_exclude_none` to `fastapi` which could cause erroneous behaviour during serialization in rare cases.

## [3.1.0]

### Added

- `module(...).had(import_=...)` construct for adding imports in older versions
- Codegen plugin system that allows easily customizing code generation for any purpose. It also significantly simplifies the core code of code generation

## [3.0.2]

### Fixed

- If a user returned a FastAPI/Starlette `Response` with an empty body, we still tried to serialize it which caused an invalid response body

## [Unreleased]

## [3.0.0]

### Added

- Pydantic 2 support
- Expanded reference section to docs
- Contributor docs
- Expanded makefile commands

### Changed

- internal request representation is now done using an annotation
- `latest_schemas_module` was renamed to `latest_schemas_package` everywhere
- `api_version_var` in `VersionBundle` is now an optional argument instead of a required one

### Removed

- `cadwyn.internal_body_representation_of` because it is now done using an annotation

## [2.3.4]

### Fixed

- `schema(...).field(...).had(ge=...)` for union fields previously raised an `AttributeError` on code generation

## [2.3.3]

### Fixed

- Field ASTs not preserving the original structure when constrained fields were changed

### Added

- Support for synchronous endpoints

## [2.3.2]

### Fixed

- The bug where fields from parent schemas also appeared in child schemas

## [2.3.1]

### Changed

- Migrate from external verselect to stable verselect

## [2.3.0]

### Fixed

- Previously whenever we generated routes, we wrapped all endpoints and callbacks in decorators for every version which caused stacktraces to be unnecessarily huge. Now there is only one wrapper for all versions

### Added

- `cadwyn.Cadwyn` class similar to `fastapi.FastAPI` that provides header routing and encapsulates all information about versioned routes
- Migrated from `fastapi-header-routing` to `verselect`
- `cadwyn.routing.VERSION_HEADER_FORMAT` from `verselect.routing`

### Changed

- `*versions` argument in `cadwyn.VersionBundle` is now split into a required positional-only `latest_version` and `*other_versions` to make it possible to see an invalid versionless definition statically. Note that it is not a breaking change because the presence of at least one version was also implicitly required before and would produce a failure at runtime

### Removed

- `cadwyn.get_cadwyn_dependency` and `cadwyn.header` because it is fully replaced with `verselect`

## [2.2.0]

### Added

- Validation for the spelling of HTTP methods in `cadwyn.structure.endpoint`.

## [2.1.0]

### Added

- A prototype of AST-based code generation where we try to keep as much of the old field/annotation structure as possible

## [2.0.5]

### Fixed

- `UploadFile` and forms have previously caused exceptions on request migration

## [2.0.4]

### Fixed

- `ContextVar[datetime.date]` being incompatible with `VersionBundle`

## [2.0.3]

### Added

- A note into reference docs about the paths specification in CLI

### Fixed

- Custom body fields created by FastAPI caused an exception. Now they are ignored

## [2.0.2]

### Added

- A link to OpenAPI discussion on enum expansion into docs/recipes
- A link to intercom's API versioning article into docs/theory

## [2.0.1]

### Fixed

- `generate_versioned_routers` did not alter `APIRoute.dependant.call`, `APIRoute.response_field`, and `APIRoute.secure_cloned_response_field` before which caused these fields to represent latest version on all generated versions. However, this was only a bug if the routes were later added into the app/router by hand instead of using `inherit_routes` or `add_api_route`.

## [2.0.0]

### Changed

- `generate_versioned_routers` now accepts only one router instead of a sequence of routers to give us the ability to guarantee that the type of generated routers is the same as the type of the passed router.

## [1.4.0]

### Added

- Theory section to docs

## [1.3.0]

### Added

- Recipes documentation section
- `schema(...).field(...).had(name=...)` functionality to rename fields

### Changed

- Tutorial example structure in tests

## [1.2.0] - 2023-10-16

### Added

- `cadwyn.main._Cadwyn` experimental private class for automatically adding the header dependency and managing all objects related to versioning

### Removed

- `cadwyn.header_routing` which only had experimental private functions

### Fixed

- Route callbacks didn't get migrated to versions with their parent routes

## [1.1.0] - 2023-10-13

### Added

- `ignore_coverage_for_latest_aliases` argument to `generate_code_for_versioned_packages` which controls whether we add "a pragma: no cover" comment to the star imports in the generated version of the latest module. It is True by default.

## [1.0.3] - 2023-10-10

### Fixed

- Add back the approach where the first version being an alias to latest in codegen

## [1.0.2] - 2023-10-09

### Fixed

- Add current working dir to `sys.path` when running code generation through CLI
- Use `exclude_unset` when migrating the body of a request to make sure that users' `exclude_unset` logic gets preserved

## [1.0.1] - 2023-10-09

### Fixed

- Pass first argument in `typer.Argument` to prevent errors on older typer versions

## [1.0.0] - 2023-10-09

### Added

- Command-line interface capable of running code-generation and outputting version info
- Internal request schema which gives us all the functionality we could ever need to migrate request bodies forward without any complexity of the prior solution
- `_get_versioned_router` and experimental header routing with it (by @tikon93). Note that the interface for this feature will change in the future

### Changed

- Renamed `cadwyn.regenerate_dir_to_all_versions` to `cadwyn.generate_code_for_versioned_packages`
- Renamed `cadwyn.generate_all_router_versions` to `cadwyn.generate_versioned_routers`

### Removed

- `unions` directory and all logic around it (replaced by internal request schema)
- `FillablePrivateAttr` and all logic around it (replaced by internal request schema)
- `schema(...).property` constructor and all logic around it (replaced by internal request schema)
- Special-casing for code generation of package with latest version using star imports from latest (replaced by internal request schema)
