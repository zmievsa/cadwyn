# Changelog

All notable changes to this project will be documented in this file.
Please follow [the Keep a Changelog standard](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Breaking

- With FastAPI 0.158.2 or newer, when a request migration supplies an omitted optional form model and other params read the form, the latest version now gets the migrated body and the untouched form fields, as in plain FastAPI. Before, the model and the other params could silently get their defaults. A non-null body supplied by the migration must be a dict with fields of the form model; otherwise, Cadwyn raises a `CadwynError` naming the param. To upgrade, make these migrations supply the model fields, and remove workarounds for the lost form values ([#529](https://github.com/zmievsa/cadwyn/issues/529)).
- With FastAPI 0.158.2 or newer, dependency form models now retain unused `AliasChoices` aliases as extra fields, like FastAPI: `v=primary&fallback=secondary` for `AliasChoices("v", "fallback")` keeps `fallback` when `v` supplies the field. Models that forbid extra fields now reject these unused aliases. Stop sending unused aliases if your model forbids them, or allow extra fields if your app needs them.

- Endpoints whose request body has a `Json[...]` field that accepts strings and other JSON values (e.g. `Json[Any]`, `Json[Union[str, int]]`, or `Json[Optional[str]]`) now get the string that the client has sent, like in FastAPI itself, in the latest API version and in the older versions whose body models are the same as the latest version's. Before, Cadwyn parsed the string as JSON once more and gave the endpoint another value with a 200, e.g. `123` instead of `"123"`, or `None` instead of `"null"`. `Json[str]` fields that get a string with JSON in it now get it as it is too (e.g. `'"x"'` instead of `"x"`). If your clients encode these values twice to get the old values, make them encode the values once. Strings that request migrations set behave as before, so migrations that encode `Json[...]` values themselves with `json.dumps()` keep working. [Known limitations](https://docs.cadwyn.dev/theory/design_decisions/#known-limitations) lists the bodies in which Cadwyn still parses these strings once more (#538).
- Removed the deprecated `openapi_prefix` argument of `Cadwyn()` (#488). FastAPI 0.145.0 had already removed it. Passing it now raises a `TypeError` on every FastAPI version, including the ones before 0.145.0 that still accept it. Set the root path on your server instead, e.g. with `root_path` or your proxy's prefix.
- Removed the deprecated changelog: the `/changelog` endpoint, `Cadwyn.generate_changelog()`, `cadwyn.hidden()`, and the `changelog_url` and `include_changelog_url_in_schema` arguments of `Cadwyn()`. Apps can generate a better and more extensible changelog from their version changes themselves, e.g. with a coding agent. To upgrade, remove these arguments from `Cadwyn()`, and remove the calls to `generate_changelog()` and `hidden()` (keep the instructions and version changes that you wrapped in `hidden()`). `Cadwyn()` now raises `TypeError` if you pass `changelog_url` or `include_changelog_url_in_schema` (#487). Cadwyn also no longer depends on `backports-strenum`, which only the changelog used on Python 3.10.
- Dropped support for Python 3.10, which reached its end of life in October 2026. Cadwyn now requires Python 3.11 or newer. To migrate, upgrade your app to Python 3.11 or newer, or stay on Cadwyn 8.x. Cadwyn no longer depends on `backports-strenum`.
- Removed the `api_version_header_name` argument of `Cadwyn()`, deprecated since 5.0.0. Passing it now raises a `TypeError`. Pass the same value to `api_version_parameter_name` instead.
- Removed `Cadwyn.add_header_versioned_routers()`, deprecated since 5.0.0. Add each version to your `VersionBundle`, describe how it differs from the latest one with version changes, and include the latest routers with `Cadwyn.generate_and_include_versioned_routers()`.
- Removed the `version_changes` attribute of `Version` and `HeadVersion`, deprecated since 4.2.0. Use `changes` instead.
- Removed `VersionBundle.version_dates`, deprecated since 5.0.0. Use `VersionBundle.version_values` instead.
- `enum(MyEnum).had(member=auto())` now uses the `_generate_next_value_` that `MyEnum` defines (#554). Before, the older versions of the enum ignored it and used the base enum type's default (e.g. powers of two for `Flag` and `IntFlag`, or lowercase names for `StrEnum`), so the new member got another value than `auto()` gives in a hand-written enum (e.g. `21` instead of `30` for a `_generate_next_value_` that returns `(count + 1) * 10`), or the versions failed to build with a `TypeError` when it returned strings. The older versions of such enums now accept and document the values that `_generate_next_value_` returns. If your clients rely on the old values, pass them explicitly, e.g. `had(member=21)`.
- When Cadwyn restores the validation aliases of a migrated request body (e.g. for `Field(alias="x", validation_alias="v")`) and the latest version rejects the restored body, Cadwyn no longer solves the latest version's dependencies a second time with the body as it was. Before, the dependencies that FastAPI doesn't cache, such as `Depends(..., use_cache=False)`, `yield` dependencies, and `Security(...)` without caching, ran once more for these requests (#563). `CadwynHeadRequestValidationError` now has the errors of the restored body, e.g. `greater_than_equal` at `("body", "v")`, instead of the errors of the body as it was, e.g. `missing` at `("body", "v")`. If your code reads these errors, e.g. in an exception handler or a test, expect the errors of the restored body. Most of these requests failed before too. Two kinds passed and now fail with these errors: requests that only passed because a dependency behaved differently when it ran a second time, and requests with a value that the latest version rejects in a field that the type's core schema gives a default even though the field has none (e.g. `Annotated[int, GetPydanticSchema(...)]` that returns `core_schema.with_default_schema(...)`). The endpoint used to get the default instead of the client's value. To keep giving it the default, delete such values from the body in a request migration, e.g. `if request.body.get("x", 10) < 10: del request.body["x"]`.
- Request and response migrations by schema (`convert_request_to_next_version_for(A)` and `convert_response_to_previous_version_for(A)`) now run for endpoints whose body or `response_model` is a union that has the schema as a member, e.g. `Union[A, B]`, `A | B`, `Optional[A]`, a discriminated union, a union of form models, or a named type alias of one (`TypeAliasType("Alias", Union[A, B])`). They run only for the requests and responses that are that member: the body that the client's version validates into it, or the object of its model (or of a subclass of it, e.g. an internal representation) that the endpoint returns. They also run for endpoints whose body or `response_model` is `A` in `Annotated` or in a named type alias (e.g. `TypeAliasType("Alias", A)`), whose responses didn't get them before. Before, they never ran for unions, and an app whose schema was only used in unions or type aliases failed at startup with `RouteRequestBySchemaConverterDoesNotApplyToAnythingError` or `RouteResponseBySchemaConverterDoesNotApplyToAnythingError` (#530). If your app has such endpoints and schema migrations for their schemas, the older versions of these endpoints now get those migrations. To upgrade:
    - Check that these migrations are right for these endpoints. If you migrated these endpoints by their paths instead, remove those migrations, or the data gets migrated twice.
    - Migrations declared for the union itself (e.g. `convert_response_to_previous_version_for(Union[A, B])`) replace the migrations of its members in the same version change. They no longer need `check_usage=False`, and request migrations declared for a union no longer fail at startup.
    - If a migration of a member would run, the following now fail with a `CadwynError`, because Cadwyn can't tell which member the response or the body is:
        - A response that the endpoint returns as anything but an object of a member's model or `None`, e.g. a dict or a database object. Cadwyn could only find the member by validating it, which would run its validators twice. Return an object of the member's model instead, or migrate the endpoint by its path.
        - A response that the endpoint returns as a `Response` object (e.g. a `JSONResponse`). Return an object of the member's model instead (e.g. `A(...)`, not the dict that you put in the `JSONResponse`), or migrate the endpoint by its path.
        - A response or a body that is an object of several members that don't inherit from each other (e.g. `class Both(A, B)` for `Union[A, B]`). Return or validate an object of only one of them (e.g. make `Both` a subclass of only one member), or migrate the endpoint by its path.
        - A union body param with an alias (e.g. `body: Union[A, B] = Body(alias="payload")`). Remove the alias, or migrate the endpoint by its path.
    - A migration of a member with `migrate_http_errors=True` now fails at startup with a `RouterGenerationError` if a union route exists in a version that it migrates, because an HTTP error isn't any member of the union. Pass `migrate_http_errors=False`, and migrate the errors of these endpoints by their paths.
- `migrate_response_body()` now restores the validation aliases of a body only if the body surely fails without them, which is the rule that routes already follow: a required field, or a model that forbids extra keys, gets a dumped key (e.g. `x` of `Field(alias="x", validation_alias="v")`) instead of its validation key. Before, it also restored them when the body failed for another reason, e.g. a model validator that rejects the default that an optional field gets instead of its value. Such bodies now raise the `ValidationError` of the body. To migrate, make your response migrations set the validation key (`v`) of such fields (#565).
- When Cadwyn restores the validation aliases of a response that fails without them, and the restored response fails too, the error is now the one of the restored response, e.g. `int_parsing` or an exception that a validator raises, instead of the `missing` error of the response as it was. This applies to route responses (`ResponseValidationError`) and to `migrate_response_body()` (`ValidationError`). Most of these responses failed before too, except the ones with the fields of the next entry. To migrate, update exception handlers and tests that check these errors to expect the errors of the restored response (#565).
- Responses with a field whose validation alias differs from its serialization alias, and that has no default in its field info but gets one from its type's core schema (e.g. `Annotated[int, GetPydanticSchema(...)]` that returns `core_schema.with_default_schema(..., default=42)`), now keep the value under the dumped key instead of silently getting the default. `migrate_response_body()` used to give the default for every value, e.g. `{"x": 42}` for `{"x": 7}`, and now gives `{"x": 7}`. Routes already kept such values. A value that the model of the version rejects, e.g. `-1` for `ge=0`, used to become the default in both routes and `migrate_response_body()`, and now raises a `ResponseValidationError` in routes and a `ValidationError` in `migrate_response_body()`. To keep giving the default, delete such values from the body in a response migration, e.g. `if response.body.get("x", 0) < 0: del response.body["x"]`, or from `latest_body` before you call `migrate_response_body()` (#565).
- Request bodies that are lists or dicts of models (e.g. `list[Model]`, `dict[str, Model]`, or `list[dict[str, Model]]`) now reach the endpoint in every version with what the client has sent, as single-model bodies and FastAPI itself do, if Cadwyn can be sure that it's right: models with `extra="allow"` keep their extra keys, only the fields that the client has set count as set (so `model_dump(exclude_unset=True)` no longer returns the defaults), the default factories of the fields that the client hasn't set run in the latest version, as they do for single-model bodies, and fields with aliases get their values. Before, they lost their extra keys, all of their fields counted as set, a required field with an alias (e.g. `Field(alias="x")` or `Field(alias="x", validation_alias="v")`) returned a 500, and an optional field with an alias that the model validates (e.g. `Field(default=0, alias="x")`) silently got its default. Request migrations still get the models of the client's version in such bodies. Everything else reaches the latest version exactly as before: models in tuples, sets, frozensets, and deques; root models, wherever they are; models that the client's version or the latest version wraps in a validator, a serializer, or `Any` (e.g. `list[Annotated[Model, BeforeValidator(...)]]`, or a field that the latest version types as `Any`); models without a model of the same class at the same place in the latest version; models whose fields, keys, or defaults the client's version changes; models with validators that get the raw input (e.g. `model_validator(mode="before")`) in either version; models whose fields share keys, or whose extra keys are keys of their fields; the whole body if any dependency of the latest version reads it in another way than the endpoint (e.g. with a validator); and every body in apps with dependency overrides (`app.dependency_overrides`). Values that the schemas don't type as models (e.g. in `Any` fields) stay as they are. `Json[...]` fields of the models that Cadwyn converts now get the values that aren't strings (e.g. `[1]` sent as `"[1]"`) instead of a 500. Cadwyn still can't tell the strings that the client has sent in such bodies from the ones that migrations set, so the fix for strings in `Json[...]` fields above doesn't apply to them: `Json[Any]` with `"\"123\""` still gets `123`, and a body with such a string anywhere stays as it is. To upgrade, check the endpoints with such bodies that use `model_extra`, `model_fields_set`, `exclude_unset`, or stateful default factories: older versions now get the same values in them as the latest one. If an endpoint needs a field to be set for older clients, set it in a request migration (#566).
- `schema(Child).field("x").had(name="y")` now fails at startup with an `InvalidGenerationInstructionError` that names the base when `Child` would still have `x` next to `y`, inherited from a base in that version or in an older one, e.g. because a parent or a mixin declares `x` and `Child` inherits it or declares it again, or because a parent that doesn't have `x` in that version gets it back in an older one. Before, the older versions of `Child` and its subclasses had both `x` and `y` in OpenAPI and in validation, so requests that sent only `y` got a 422 while `x` was required (#525). Apps with such an instruction no longer start. To fix this:
    - To rename the field in the parent and all of its subclasses, give the instruction to the schema that declares it: `schema(Parent).field("x").had(name="y")`. If `Child` declares `x` again, or changed `x` in a newer version, rename it in both `Child` and the parent in the same version. Otherwise, Cadwyn fails at startup, as the next entry describes.
    - To keep the older versions exactly as they were, with both fields, replace the instruction with `schema(Child).field("y").existed_as(type=..., info=Field(...))`, passing the type and the `Field()` attributes that `Child.x` has. If `Child` declares `x` again, also make `x` in `Child` the same as the parent's `x` in that version: `schema(Child).field("x").had(type=..., ...)` with the parent's type and `Field()` attributes, and `schema(Child).field("x").didnt_have(...)` for the attributes that only `Child` sets, including the constraints in `Annotated`.
- `schema(Parent).field("x").had(name="y")` now fails at startup with an `InvalidGenerationInstructionError` when a subclass of `Parent` would still have `x` next to `y` in that version: a subclass that declares `x` itself (e.g. `x: Literal["a"]`), that changed `x` in a newer version (e.g. `schema(Child).field("x").had(description="...")`), or that gets `x` from another base (e.g. a mixin). This also applies when the subclass renames `x` to another name than `y` in the same version. Before, the older versions of such subclasses had both names in OpenAPI and in validation, and both were required unless they had defaults (#581). Subclasses that only inherit `x` from `Parent` follow the rename as before. To fix this, rename the field in the subclass too, or in the other base that gives it `x`, in the same version, e.g. `schema(Child).field("x").had(name="y")`.
- `schema(Child).field("x").didnt_exist` now fails at startup with an `InvalidGenerationInstructionError` that names the base (or all of the bases that declare `x`, if Cadwyn can't tell which one pydantic took it from) when `Child` would still inherit `x` from a base in that version, e.g. because `Child` declares `x` again or changed it in a newer version while its parent still has `x`. Before, the instruction silently did nothing: the older versions of `Child` had the parent's `x` (#582). To fix this, delete the field from the parent too, in the same version, which deletes it from all of its subclasses.
- Request bodies that FastAPI doesn't parse as JSON are no longer parsed as JSON by Cadwyn either (#531). With FastAPI's default `strict_content_type=True`, FastAPI parses a non-empty body as JSON only if the request has a JSON `Content-Type` (`application/json` or `application/*+json`). Cadwyn also parsed it as JSON when the request had no `Content-Type` header or an empty one. Now Cadwyn reads these bodies like FastAPI does, as raw bytes, in every API version. Request migrations get what FastAPI has read instead of the parsed JSON, the same as they already did for `Content-Type: text/plain`. The endpoint and its dependencies get the same values as they would without Cadwyn. Where Cadwyn already passed on FastAPI's values (e.g. for a single body param without an alias that the endpoint declares itself), nothing changes. For example:
    - a migration that reads or changes the body as a dict (e.g. `request.body["a"]`) now fails with a 500 error
    - params that FastAPI reads from the body by key (several body params such as `a: int = Body(0)` and `b: int = Body(0)`, a `Body(embed=True)` param, or a body model next to other body params) now get their defaults instead of the values from the JSON. Required params already returned a 422 error
    - a body param that FastAPI validates from the whole body, but whose value Cadwyn took from the parsed JSON (e.g. a param with an alias such as `a: str = Body(alias="wire")`, or one that only a dependency declares), now gets the raw body. For example, the body `"hello"` now gives the string `"hello"` with its quotes instead of `hello`
    - for params read by key, a body that isn't valid JSON no longer returns a 422 error (`json_invalid`), and a JSON list no longer returns a 500 error

    Requests with a non-empty `Content-Type`, requests with an empty body, form endpoints, and requests whose effective `strict_content_type` is `False` behave as before. The effective value is the one that FastAPI uses for the route: the route's own value, or else the one of the closest router or the app that sets it. To migrate, make the clients send `Content-Type: application/json`. If you can't change the clients, set `strict_content_type=False` on `Cadwyn()`, on a router, or on the route. Then both FastAPI and Cadwyn parse bodies without a `Content-Type` as JSON again.
- In every API version, the endpoint now gets the latest members of a plain enum or flag (e.g. `class Kind(Enum)`, not `class Kind(str, Enum)`) that has `enum(...)` instructions, and the enums of older versions take the latest members that have exactly the value of one of their members. `str`, `int`, and `float` enums behave as before. This changes three things that work today (#521):
    - A request body field that takes the enum or another type, such as `value: Union[Kind, int]`, now gets the member (`Kind.a`) instead of its value (`"a"`), like in FastAPI itself. If your endpoint compares this field to the value, compare it to the member instead (`body.value is Kind.a`).
    - If the enum has a `_missing_` that returns a member for any value (e.g. `unknown`), the endpoint now gets the member that the client sent (`Kind.a`), and the response has the value of the member that the endpoint returned (`"a"`), instead of that member. `_missing_` still gets the members that the other version's enum has no member with the same value for. If you relied on getting that member for the values that the enum has, map them to it in a request or response migration.
    - Calling the enum of an older version with a latest member that has the value of one of its members (e.g. `OldKind(Kind.a)`, where `OldKind` is the enum that Cadwyn generated for that version) now returns that member instead of raising a `ValueError` or calling the enum's `_missing_`. For a flag, this includes the combinations of its members, but not values with bits that it doesn't have. To tell the latest members apart, check `isinstance(member, Kind)`.
- With FastAPI 0.153.0 or newer, for endpoints that take a union of form models (e.g. `body: Annotated[Union[ModelA, ModelB], Form()]`), a list, set, or tuple that a validator or a migration puts into the body now reaches the endpoint as it is, like in FastAPI itself, unless the models of the union take the field differently ([Known limitations](https://docs.cadwyn.dev/theory/design_decisions/#known-limitations) lists these cases) (#532). Requests that worked before change in these cases:
    - With FastAPI 0.153.0 to 0.160.x, a list in a field that a model of the union takes as a single value (e.g. `value: Any`) gave that model only the last item of the list. Now it gets the whole list.
    - A one-item list in an extra field of a model that allows extra fields (`extra="allow"`) gave the model the item instead of the list. Now it gets the list.
- A param named `body` is no longer mistaken for the whole request body (#526). This affects endpoints with files or forms, with `Body(embed=True)`, or with several body params, and endpoints whose body alias is the name of a dependency. Before, uploads named `body` (e.g. `body: bytes = File()` or `body: UploadFile = File()`) and uploads next to a query, header, cookie, path param, or dependency named `body` returned a 500 or arrived as `None`. A dependency named `body` (or named after the body's alias) whose value differed from the body replaced the body, so the endpoint got that value. Now the endpoint gets what FastAPI gives it in every version. Migrate your app if:
    - your request migrations by path for such endpoints read the value of the `body` param or dependency from `request.body`. They now get the request body as the client sent it, e.g. the form (`request.form`), like for other endpoints with forms.
    - you relied on a dependency named `body` to replace the uploads or the body that the endpoint gets, e.g. `upload: UploadFile = File(), body = Depends(replace_upload)`. Change the upload in a request migration, or let the endpoint read the dependency's value instead.
- Computed fields of request bodies no longer reach the latest version as input (#570). Before, Cadwyn passed the values of the computed fields of a model body (e.g. `body: MyModel`, a `RootModel`, a union of models, or a form model) and of the models nested in it to the latest version, as FastAPI never does: a model with `extra="allow"` got them as extra keys, a model with `extra="forbid"` returned a 500 on every request, and a computed field with the key of a field (e.g. `@computed_field(alias="name")`) replaced the client's value of that field. Request migrations no longer see the computed fields in `request.body` either. To upgrade, compute such values in the migrations that read them from `request.body`, and stop reading them from `model_extra` in your endpoints. Responses still have their computed fields. Bodies whose models override `model_dump()` (e.g. SQLModel models) are dumped as before, with their computed fields.
- Cadwyn now requires Pydantic 2.12.0 or newer, whose `model_dump()` can leave out computed fields (#570). To upgrade, upgrade Pydantic to 2.12.0 or newer.
- With FastAPI 0.158.2 or newer, the form fields and embedded body params (`Body(embed=True)`) of dependency overrides and other dependencies next to an endpoint's form model now get the value that FastAPI gives them for the migrated request (#527). This changes what some of them got before:
    - A param that got the whole form model, e.g. `body: Any = Form()` next to `body: Annotated[MyModel, Form()]` when the form has a `body` field that the form model doesn't take, now gets its own value from the form. If a request migration removes the key that a required param reads, and the form has no other value for it, the request fails with a 500 (`CadwynHeadRequestValidationError`), as FastAPI rejects a form without it.
    - If a request migration removes the key of a form model field that a param reads, the param now gets the value of its next alias that the form has (e.g. `"external"` of `Form(validation_alias=AliasChoices("text", "external"))`) instead of its default.
    - If Cadwyn can't give a param exactly that value, the request now fails with a 500 (`CadwynError`) that names the param, instead of giving it another value. This happens for every param that takes the whole body (e.g. `payload: dict = Body()` in an override), which would also see the form model's data under the form model's key, when a dependency declares the endpoint's form model and another param has its alias (e.g. `text: Annotated[MyModel, Form()]` in a dependency and `text: str = Form()` in an override), and, after a request migration, when a param reads a key that the form model has taken under another name (e.g. through `AliasChoices`), when the migrated body of the form model isn't a dict, or when it has a list, a set, or a tuple under a key that a union of form models of a dependency reads. To fix it, give the params other names or aliases, or read the form from the `Request` instead of a whole-body param.
- `schema(...).field(...).had(alias=...)` and `.didnt_have("alias")` now also change the `validation_alias` and `serialization_alias` that Pydantic copied from the field's alias, like `Field()` does. So older API versions accept and return the key that their OpenAPI documents. Before, these aliases kept the old alias, so the instruction usually changed nothing in requests, responses, or OpenAPI, only in `cadwyn render`. The `FutureWarning` that Cadwyn 8.1 added for these instructions is removed (#465).
  - **Affected apps:** apps with such an instruction for a field declared with an alias whose `validation_alias` or `serialization_alias` Pydantic copied from it because it wasn't passed, or was passed with the same value. Only the copied ones change, and only if no instruction sets or deletes them explicitly for that version. For example, with `.had(alias="legacy")`, `Field(alias="a")` and `Annotated[str, Field(alias="a")]` change their keys in requests and responses, `Field(alias="a", serialization_alias="s")` changes only its request key, and `Field(alias="a", validation_alias="v", serialization_alias="s")` doesn't change. These are the instructions that Cadwyn 8.x warned about at startup, plus the ones for models with an `alias_generator` that doesn't override the field's alias, and for fields whose `validation_alias` is an `AliasChoices` or `AliasPath` (their responses change), which it didn't warn about. Like any `.didnt_have()`, `.didnt_have("alias")` keeps the alias of a `Field()` in `Annotated`, so such a field keeps its keys, unless its module uses `from __future__ import annotations`.
  - **What changes for their clients:** in the older versions, the field uses the new key, i.e. the alias from `.had(alias=...)`, or the field name (or the key from the `alias_generator`) after `.didnt_have("alias")`. A request that has the old key but not the new one gets a 422, and a response that has the old key but not the new one fails with a `ResponseValidationError` (a 500), e.g. a `GET` whose response migration is missing. Before 9.0, these values arrived and were returned under the old key. Cadwyn decides this from the data as it comes in, before any validators run. So this also fails when a validator of the model fills the field from other keys, e.g. a `model_validator(mode="before")` that copies a `compatibility_name` key into it, if the data has the old key but not the new one. Requests that have the new key need a request migration as for any other renamed key: without it, the latest version doesn't get their value, which fails for a required field and gives an optional field its default.
  - **Migration:** to use the new key, add a request migration that renames it to the key of the newer version and a response migration that renames it back, as in [the example](https://docs.cadwyn.dev/concepts/schema_migrations/#change-a-field-in-the-older-version). To keep the old key, remove `alias=...` from the `.had()` (or `"alias"` from the `.didnt_have()`). `HeadVersion` can't have migrations, so remove alias changes from it.
  - Cadwyn now also accepts `.had(alias=..., validation_alias=..., serialization_alias=...)` that keeps the key of the newer version, e.g. `.had(alias="legacy", validation_alias="a", serialization_alias="a")` on `Field(alias="a")`, which it rejected before because the field "already has that value".
- The enums that Cadwyn generates for older versions of an enum with `enum(...)` instructions now behave like the enum that you would write by hand for that version (#555). This changes these enums in older versions:
    - Aliases (e.g. `b = 1` next to `a = 1`) and the multi-bit aliases of a `Flag` (e.g. `read_write = 3` next to `read = 1` and `write = 2`) are now aliases of their member, and `didnt_have()` can remove them. Before, each alias was a separate member whose value was the latest enum's member. So OpenAPI listed a value such as `"Kind.a"` for an alias of a `str` enum, and requests with that string returned a 500 error instead of a 422 error. OpenAPI now lists the values of the members and aliases in the order of their definition, as it does for the latest version (e.g. `[1, 2, 3]` instead of `[3, 1, 2]` for the flag above).
    - Attributes declared with `nonmember()` are no longer members. Before, OpenAPI listed their values, and requests with them returned a 500 error. Now they return a 422 error, like in the latest version.
    - Functions declared with `@member` are now members, like in the latest version. Before, they were plain methods.
    - An enum with a custom `__new__` of its own or inherited from your base enum (not the `__new__` of `Enum`, `StrEnum`, `IntEnum`, `Flag`, `IntFlag`, or `ReprEnum`) now keeps the attributes that `__new__` and `__init__` set on its members. Members that have nothing but their value (no attributes or slots, and for a mixin type such as `str`, data equal to the value) are copied with their exact value without calling `__new__`, as before. Cadwyn only knows the values of the members, not the arguments that they were defined with, so it creates the other members by calling `__new__` with their values, and compares the result to the enum by the same rules as `had(...)`, so e.g. `Decimal("1.0")` differs from `Decimal("1.00")`. The only exception is new objects of `date`, `time`, `datetime`, `timedelta`, `UUID`, and `pathlib` paths that are equal to the original ones and have the same text, e.g. a `date` that `__init__` makes again. If they differ (e.g. for `__new__(cls, value, mass)` with `earth = (1, 5.97)`), Cadwyn now raises a `SchemaGenerationError` at startup. Before, the older versions silently dropped an enum's own `__new__`, so the attributes that it sets (e.g. `Planet.earth.mass`) were missing, and silently got other values from an inherited one (e.g. `0` from a default argument instead of `5.97`), or failed with a `TypeError`. Members that `had()` adds now go through `__new__` like in the class body: `had(venus=(3, 4.87))` gives `venus` the value `3` and the attributes that `__new__` sets, instead of the value `(3, 4.87)` without them. To migrate, compute such attributes from the value, e.g. with a property (see [Enum migrations](https://docs.cadwyn.dev/concepts/enum_migrations/#how-cadwyn-copies-enums)).

### Changed

- The type hints of `convert_request_to_next_version_for()` and `convert_response_to_previous_version_for()` now accept unions of schemas (e.g. `Union[A, B]` or `A | B`), so type checkers no longer reject migrations declared for a union (#530).
- `schema(...).field(...).had(...)`, `enum(...).had(...)`, and `endpoint(...).had(...)` now treat a value as unchanged only when Cadwyn is sure that it is the same as the current one, and apply the instruction otherwise. Before, Cadwyn compared the values with `==`, so it rejected real changes such as `default=True` when the default is `1`: it raised `InvalidGenerationInstructionError` (or `RouterGenerationError` for endpoints) saying that the attribute already has that value, and the app failed to start or returned a 500 error for every request. Now the older version gets the new value, in OpenAPI too. A value is the same as the current one only if it is the same object, or if both have exactly the same type and are:
  - `None`, `bool`, `int`, `str`, or `bytes` values that are equal
  - `float` values that are equal and have the same sign, so `-0.0` differs from `0.0`, and NaN differs from every other NaN object. `complex` values compare their real and imaginary parts in the same way
  - `Decimal` values with the same digits and exponent, so `Decimal("1.0")` differs from `Decimal("1")`, and NaN differs from every other NaN object
  - `list` and `tuple` values with the same items in the same order, and `dict` values with the same keys and values in the same order. Items are compared by these same rules, so `[1]` differs from `[True]`
  - `set` and `frozenset` values with the same items, if all items of both sets are values of the types above or `tuple` and `frozenset` values of them. So `frozenset({True})` differs from `frozenset({1})`. A set with any other item, e.g. an enum member or a model instance, is compared by identity
  - these dataclasses of pydantic, FastAPI, and annotated_types with the same fields by these rules: pydantic's `AliasChoices`, `AliasPath`, `Discriminator`, `StringConstraints`, `Strict`, `AllowInfNan`, and `FailFast`, FastAPI's `Depends` and `Security`, and annotated_types' `Gt`, `Ge`, `Lt`, `Le`, `MultipleOf`, `MinLen`, `MaxLen`, `Len`, `Interval`, `Timezone`, and `Predicate`
  - type hints from `typing` and `types` with the same origin, the same args in the same order, and the same other attributes, so `Union[int, str]` differs from `Union[str, int]`, `Literal["b", "a"]` from `Literal["a", "b"]`, and `tuple[*tuple[int, ...]]` from `tuple[tuple[int, ...]]`. Classes in them are compared by identity

  Everything else is compared by identity: subclasses of `list`, `tuple`, `dict`, `set`, and `frozenset`, instances of all other dataclasses and classes (e.g. pydantic models, enum members, `datetime`, and `UUID`), and values that refer to themselves. Cadwyn runs none of your code to compare values: it never calls `__eq__`, `__hash__`, properties, or metaclass methods of your objects. So Cadwyn now also applies instructions that it rejected before, e.g. `had(default=MyDataclass(x=1))` when the default is another `MyDataclass(x=1)` object, and an enum member value of `True` instead of `1`. Such an instruction may change nothing, which is harmless. Apps that work today need no changes. The error for a field attribute now says `of "MyModel"` instead of `from "MyModel"`, so it no longer reads as if the model name were the old value (#541).
- `had(...)` now rejects a value that is the very same object as the current one, even if the object isn't equal to itself, e.g. `schema(MyModel).field("x").had(default=math.nan)` when the default of `x` is already that `math.nan` object. Before, Cadwyn applied it, which changed nothing. To migrate, remove the instruction.

### Fixed

- Responses whose validation aliases Cadwyn restores now go through the validators of their models once, like in FastAPI without Cadwyn. Before, routes validated the restored response and FastAPI validated it again, so `mode="after"` model validators ran twice, and `migrate_response_body()` validated the body as it was and then the restored body, so the validators of the fields that passed ran twice. Validators with side effects, such as one that increments a value, gave wrong values (#565).
- Request and response bodies with a plain enum or flag (e.g. `class Kind(Enum)` or `class Permission(Flag)`) that has `enum(...)` instructions no longer fail with a 500 error in every API version, including when the enum is in lists, tuples, sets, frozensets, deques, dicts, `OrderedDict`s, `defaultdict`s, or `Counter`s (as keys or values), nested models of a model body, or the extra fields of these models. Request bodies that aren't a single model but hold models with the enum (e.g. `body: list[Item]`), and bodies with the enum in an `Iterable[...]` field, in a model that a migration puts into the body, or in a container that pydantic doesn't make (e.g. a custom sequence that a migration puts into the body), still fail as before, because Cadwyn leaves such objects as they are (see [Known limitations](https://docs.cadwyn.dev/theory/design_decisions/#known-limitations)). Request migrations still get the members of the enum of the request's API version. A member whose value the latest version doesn't have still fails, as before, and so does a flag value with bits that the other version's enum would drop (e.g. with `boundary=CONFORM`). On FastAPI versions before 0.148, a query, header, cookie, or form param with a plain enum member as its default (e.g. `kind: Kind = Query(Kind.a)`) no longer fails with a 422 in older API versions when the request omits it, unless the version doesn't have that member (#521).
- With FastAPI 0.161.0 or newer, endpoints that take a union of form models no longer fail with a 500 error when the body has a list, set, or tuple in a field that a model of the union takes as a single value or as an extra field, e.g. a field of type `Any` that a validator or a migration fills with a list. Endpoints that take an optional union of form models (e.g. `body: Annotated[Optional[Union[ModelA, ModelB]], Form()] = None`) get the form data in this case instead of `None`. The endpoint gets the value, like in FastAPI itself. If a model of the union takes the field as a single value and another one takes it as repeated values or as an extra field, or a form model outside the union (e.g. of a dependency override) shares the body, the request still fails, but with a `CadwynError` that names the field and the models instead of a `CadwynHeadRequestValidationError` (#532).
- With FastAPI 0.153.0 to 0.160.x, endpoints that take a union of form models no longer fail with a 500 error when a model of the union has a `set`, `frozenset`, or `tuple` field (#532).
- Copying a model with a `Final[...]` annotation (e.g. `x: Final[int] = 1` in a module without `from __future__ import annotations`) no longer crashes at startup with a `TypeError` when the model or its subclass has schema instructions. The same goes for the other forms that take a single type, such as `TypeGuard` and `TypeIs` (e.g. in `Callable[[Any], TypeGuard[Item]]`) and `ClassVar[Final[...]]` (#576).
- Background tasks can now read the `UploadFile` params of a request. Before, Cadwyn closed the uploaded files when the endpoint returned, so the task failed with `ValueError: I/O operation on closed file`. Cadwyn now closes the uploads of versioned routes once, after the response and its background tasks, also when the client disconnects or the request gets cancelled.
- Uploads that request migrations put into the form (e.g. `request.form[:] = [("upload", UploadFile(...))]`) are now closed after the request. Before, nothing closed them, so their files stayed open.
- Background tasks can now read the `UploadFile` params of a request. Before, Cadwyn closed the uploaded files when the endpoint returned, so the task failed with `ValueError: I/O operation on closed file`. Cadwyn now closes the uploads of versioned routes once, after the response and its background tasks, also when the client disconnects or the request gets cancelled, and also for the routes of `generate_versioned_routers()` in an app that isn't a `Cadwyn` app. It gives each upload 3 seconds to close, and logs a warning for an upload that doesn't close in time, so that such an upload can't keep the request running.
- Uploads that request migrations put into the form (e.g. `request.form[:] = [("upload", UploadFile(...))]`) are now closed after the request, including the ones that later migrations replace or remove. Before, nothing closed them, so their files stayed open.
- `vars()` and `__dict__` of the members of the enums that Cadwyn generates for older versions of a `str`, `int`, or other mixin enum (e.g. `class Kind(str, Enum)`) with `enum(...)` instructions no longer raise a `TypeError`, and enums that declare their own `__slots__` no longer fail to copy with a `ValueError` (#555).

### Added

- `Cadwyn()` and `generate_versioned_routers()` now take `run_background_tasks_of_latest_version_dependencies`. With `True`, the background tasks that dependencies add while Cadwyn solves the dependencies of the latest version run after the response, as in FastAPI. Without it, Cadwyn keeps these tasks only if the endpoint itself declares a `BackgroundTasks` param, and silently drops them otherwise. With the flag, Cadwyn adds a `BackgroundTasks` param named `cadwyn_background_tasks_param` to the endpoints that don't declare one. It shows up in the endpoint's signature and in `route.dependant.background_tasks_param_name`, but not in OpenAPI (#568).

### Deprecated

- Dropping the background tasks that dependencies add while Cadwyn solves the dependencies of the latest version. Cadwyn still does it by default when the endpoint doesn't declare a `BackgroundTasks` param, but now emits a `FutureWarning` once per route when it drops such a task. It warns in a worker thread after FastAPI has sent the response and run its background tasks, so requests that fail don't warn. If your warning filters turn the warning into an error, Cadwyn logs it instead, and ignores errors of logging. Tasks that a dependency adds only after the endpoint returns, e.g. after the `yield` of a dependency with `scope="function"`, are dropped without a warning. Without the flag, routes, endpoint signatures, and responses stay as before. Cadwyn 10.0 will run these tasks by default. To switch now, pass `run_background_tasks_of_latest_version_dependencies=True` to `Cadwyn()`. Dependencies run for both the request's version and the latest version, so a dependency that adds a task in both runs will then have it run twice. To keep a single run, add the task only when `current_dependency_solver` is `"fastapi"` (#568).
- Container defaults now keep their types and attributes in every version, including the latest one (#564). This covers the defaults and other attributes of fields (e.g. `examples` and `json_schema_extra`), the defaults of endpoint and dependency params, and the metadata of `Annotated`. Before, Cadwyn rebuilt every list, tuple, and dict in them in every version, so subclasses of `list` and `tuple` lost their attributes, subclasses of `dict` (e.g. `OrderedDict`, `Counter`, `defaultdict`, or your own class) became plain dicts, and `NamedTuple` defaults and lists that contain themselves failed with a `TypeError` or a `RecursionError`. Plain lists, tuples, and dicts are still rebuilt as before. Every other subclass of `list`, `tuple`, or `dict` (including `NamedTuple`, `OrderedDict`, `Counter`, and `defaultdict`), and every list, tuple, or dict that contains itself, now gets its own `copy.deepcopy()` in every version, as Pydantic gives every instance its own deep copy of a mutable default. If `deepcopy` fails, Cadwyn raises a `SchemaGenerationError` that names the field. If such a container holds a model class (every version has its own copies of all models), an enum class with instructions, a function or another callable (except the ones from FastAPI, Pydantic, and Starlette, and the ones without an inspectable signature), or a type that contains one of them (e.g. `list[Item]` or `Annotated[int, Item]`), Cadwyn raises a `SchemaGenerationError` that names the field instead of rebuilding the container into a different value. Before, it became a plain list, tuple, or dict that held the version's copies, or failed with a `TypeError` for a `NamedTuple` or a `RecursionError` for a container that contains itself. To migrate, use a plain `list`, `tuple`, or `dict` for such containers. A default factory whose defaults contain a container that leads back to the factory (e.g. a dict of factories) now fails with a `SchemaGenerationError` instead of a `RecursionError`.
- Container defaults now keep their types and attributes in every version, including the latest one (#564). This covers the defaults and other attributes of fields (e.g. `examples` and `json_schema_extra`), the defaults of endpoint and dependency params, and the metadata of `Annotated`. Before, Cadwyn rebuilt every list, tuple, and dict in them in every version, so subclasses of `list` and `tuple` lost their attributes, subclasses of `dict` (e.g. `OrderedDict`, `Counter`, `defaultdict`, or your own class) became plain dicts, and `NamedTuple` defaults and lists that contain themselves failed with a `TypeError` or a `RecursionError`. Plain lists, tuples, and dicts are still rebuilt as before. Every other subclass of `list`, `tuple`, or `dict` (including `NamedTuple`, `OrderedDict`, `Counter`, and `defaultdict`), and every list, tuple, or dict that contains itself, now gets its own `copy.deepcopy()` in every version, as Pydantic gives every instance its own deep copy of a mutable default. If `deepcopy` fails, Cadwyn raises a `SchemaGenerationError` that names the field. The same now goes for the attributes of the fields of models with instructions, which every version always deep copied: they fail with this error instead of, e.g., a `TypeError`. If such a container holds a model class (every version has its own copies of all models), an enum class with instructions, a function or another callable (except the ones from FastAPI, Pydantic, and Starlette, and the ones without an inspectable signature), or a type that contains one of them (e.g. `list[Item]` or `Annotated[int, Item]`), Cadwyn raises a `SchemaGenerationError` that names the field instead of rebuilding the container into a different value. Before, it became a plain list, tuple, or dict that held the version's copies, or failed with a `TypeError` for a `NamedTuple` or a `RecursionError` for a container that contains itself. To migrate, use a plain `list`, `tuple`, or `dict` for such containers. A default factory whose defaults contain a container that leads back to the factory (e.g. a dict of factories) now fails with a `SchemaGenerationError` instead of a `RecursionError`.

### Fixed

- Request bodies with `Json[...]` fields that get a string or a member of a string enum (e.g. `Json[str]`, `Json[Any]` with `"\"abc\""`, or `Json[Kind]` for `class Kind(str, Enum)`) no longer fail with a 500 error in the latest API version and in the older versions whose body models are the same as the latest version's, including when a request migration has encoded other `Json[...]` values of the body itself (#538).
- With FastAPI 0.158.2 or newer, the form fields and embedded body params of dependency overrides next to an endpoint's form model (e.g. `extra: str = Form()` in an override next to `body: Annotated[MyModel, Form()]`) now get their values from the form, as in FastAPI itself. Before, they got their defaults or the request failed with a 500 when the form model, or the request's version of it, doesn't have the field, or when the param has `AliasChoices`, takes a list, or takes `bytes` uploads. Requests that have only such fields and none of the fields of an optional form model (e.g. `body: Annotated[Optional[MyModel], Form()] = None`), or none at all, no longer fail with a 500: the form model gets its default, and the other params get their values from the form (#527).
- With FastAPI 0.158.2 or newer, form models that dependency overrides or other dependencies declare next to an endpoint's form model with other models (e.g. `other: Annotated[OtherModel, Form()]` in an override) now get their fields from the form, as in FastAPI. Before, they got the endpoint's form model data, so their fields got their defaults, or the request failed with a 500. A form field whose alias is a later alias of a form model field's `AliasChoices` (e.g. `fallback` of `AliasChoices("v", "fallback")` when the form also has `v`) now gets its value from the form instead of its default (#527).
- With FastAPI 0.158.2 to 0.159.1, an optional form model that allows or forbids extra fields (e.g. `body: Annotated[Optional[MyModel], Form()] = None` with `extra="allow"`) no longer fails with a 500 when the form has only fields that other params declare, e.g. a form field of a dependency override. The form model gets its default, as in FastAPI, which doesn't take such fields as extra fields of the form model (#527).

## [8.2.0]

### Possibly breaking

- With FastAPI 0.158.2 or newer, a form field of a dependency override that has the same name as the endpoint's form model param and accepts any value (e.g. `body: Any = Form()` next to `body: Annotated[MyModel, Form()]`) now gets its own value from the form instead of the whole form model, like in FastAPI itself (#496).

### Added

- The README and the documentation now link to the author's blog and X.
- Documented how Cadwyn works inside, for contributors, on the new "Architecture" page in the "Theory" section of the documentation (#202).
- [Design decisions and trade-offs](https://docs.cadwyn.dev/theory/design_decisions/) now lists more known limitations, more rules for changing Cadwyn, and how to check a change (#202).

### Fixed

- The `cadwyn` command can import modules from the current directory again, so `cadwyn render module data.schemas --app=main:app --version=...` works when run from the folder that contains `main.py` and `data/`. Before, it failed with `ModuleNotFoundError` unless it was run as `python -m cadwyn`. Cadwyn 1.0.2 had fixed this, but later versions stopped putting the current directory on `sys.path`.
- The how-to examples for changing schemas are now tested, which removed their unused imports and added the missing import of the version changes in the "Narrow the type" example.
- Fixed documentation examples that failed or did the opposite of what they described: reversed field renames in the data migration and rename-a-field examples, the `migrate_response_body()` call without its `versions` argument, swapped "adding" and "removing" enum member examples, a `VersionChangeWithSideEffects` example without `instructions_to_migrate_to_previous_version`, a `hidden()` example that changed path params (which Cadwyn rejects), a "remove a required field" recipe that left HEAD without the default it needs, and missing or wrong imports in several examples.
- The examples on the "Version changes", "Changelogs", and "Enum migrations" documentation pages are now tested. This fixed the example of migrating HTTP errors, which changed 400 to 404 instead of changing the latest 404 back to 400, the example of returning an internal representation, which unpacked an object as a dict, and missing imports in the `current_dependency_solver` example. The "Version changes" page also no longer suggests renaming an inherited field in each child schema instead of the parent, which keeps the old field next to the new one. The "Enum migrations" examples now show the enum they change (`class MyEnum(str, Enum)`).
- The code examples on the "Schema migrations", "API version parameter", "Schema generation", "Endpoint migrations", and "Main App" pages are now tested. Examples that could not run now work as written: they no longer import from hypothetical `versions` and `my_versions` modules or pass `...` to `Cadwyn()`, and the validator examples no longer have unused imports. The "Schema migrations" page now says to rename an inherited field in the schema that declares it, because renaming it in a subclass keeps the old field next to the new one. The `cadwyn render` examples on the "Schema generation" page now say to run them from the app's directory.
- Fixed documentation claims that contradicted Cadwyn's behavior: that Cadwyn only supports date-based versions, that `api_version_parameter_name` defaults to `"X-API-VERSION"`, that a schema can have both `default` and `default_factory`, that validators can reject values with `TypeError`, and references to the `data.vXXXX_XX_XX` packages that Cadwyn no longer generates.
- With FastAPI 0.158.2 or newer, endpoints with an optional form model (e.g. `body: Annotated[Optional[MyModel], Form()] = None`) no longer fail with a 500 error in every API version when the request has none of the model's form fields. Request migrations get `None` as the body, and the endpoint gets the param's default (#495).
- With FastAPI 0.158.2, endpoints that take a form model no longer fail with a 500 error, and endpoints that take an optional form model now get its form data instead of `None`. Cadwyn handled the way FastAPI looks up form models only from FastAPI 0.159.0, while FastAPI changed it in 0.158.2 (#495).
- With FastAPI 0.158.2 or newer, a form field or an embedded body param (`Body(embed=True)`) of a dependency override that has the same name as the endpoint's form model param (e.g. `body: Annotated[MyModel, Form()]` in the endpoint and `body: str = Form()` in the override) now gets its own value from the form model's data instead of the whole form model, like in FastAPI itself. If the data has no value for it, it gets its default. Before, this failed with a 500 error, or, if the param accepted the form model's data (e.g. `body: Any = Form()`), gave it the whole form model. Requests in which another param of an override takes the whole body, a required param has no value in the data, or the form has a value for a param that the form model of the request's API version doesn't have a field for (e.g. a field that only newer versions of the model have, or, for a union of form models, a field that only the members that the request doesn't match have), work as before (#496).
- With FastAPI 0.161.0 or newer, endpoints that take a union of form models (e.g. `body: Annotated[Union[ModelA, ModelB], Form()]`) no longer fail with a 500 error in every API version, including when the models have `set`, `frozenset`, or `tuple` fields. Endpoints that take an optional union of form models (e.g. `body: Annotated[Optional[Union[ModelA, ModelB]], Form()] = None`) now get the form data instead of `None`, and no longer fail with a 500 error when the request has none of the models' form fields. Request migrations of these endpoints get the form data of the model that the request matches as the body, or `None` if the request has no form data for the optional union. Requests whose body has a list, set, or tuple in a field that a model of the union takes a single value for (e.g. a field of type `Any` that a validator or a migration fills with a list, or a field that a model which allows extra fields doesn't have) behave as before, because FastAPI would give the field only one of the values.
- On Python 3.11 and newer, `enum(...)` instructions on a `Flag` or `IntFlag` enum no longer make every route and the OpenAPI return a 500 error. The enum of each version has the members of that version, combines them like a hand-written enum, and keeps the enum's boundary (#522).
- Request bodies, responses, and fields typed by a native `type` alias that refers to itself (Python 3.12+), directly or through other type aliases, no longer fail with a 500 error in every API version when the alias only contains builtin types, such as `type JSON = dict[str, JSON] | list[JSON] | str | int | None`, and other such aliases. Recursive type aliases that contain anything else, such as models, enums, other classes, forward refs, or `Annotated`, still can't be versioned: instead of a `RecursionError`, the first request now fails with a `SchemaGenerationError` that names the alias (#524).
- With FastAPI 0.158.2 or newer, when a request has none of the fields of an optional form model param without an alias (e.g. `body: Annotated[Optional[MyModel], Form()] = None`), or, with FastAPI 0.161.0 or newer, of an optional union of form models, a dict that a request migration sets as the body (e.g. `request.body = {"name": "x"}`) now reaches the endpoint, the dependencies that declare the same form model under the same param name, and the dependency overrides that declare the same form model. Before, they got `None`, or a 500 error for some unions. A body that the form model rejects now fails with a 500 error, as it does when the form supplies the model. Everything else behaves as before, including a dict with none of the form model's fields (e.g. `{}`), which gives the param its default, as FastAPI does for a form, a body that isn't a dict or that has a key named like the param (e.g. `{"body": {"name": "x"}}`), another form model or form field with the same name as the param, a param that gets the whole body (e.g. `payload: dict = Body()` in an override of a dependency that only the latest version has), a form field that isn't a form model (e.g. `x: str = Form("d")` in an override), a dependency that declares a form model under another param name, a key that another param reads next to a form model that allows or forbids extra fields, and, for a union, a body with values that FastAPI would change when it validates them as form data (e.g. an empty string, or a one-item list in an extra field) (#529).
- Request bodies with `Json[...]` fields (e.g. `data: Json[dict]`), including form models and nested models, no longer fail with a 500 error in the latest API version and in the older versions whose body models are the same as the latest version's. Request migrations still get the parsed values of these fields. If Cadwyn can't be sure that the latest version gets what the client has sent everywhere in the body, the request behaves as before. [Known limitations](https://docs.cadwyn.dev/theory/design_decisions/#known-limitations) lists these cases (#523).

## [8.1.1]

### Added

- The documentation now has a separate version for each Cadwyn release, with a version selector in the header. `latest` points to the newest release and `dev` to the `main` branch.

### Fixed

- When the app has a root path (e.g. `--root-path /prefix`, or a parent app with a root path that mounts the Cadwyn app), the version links on `/docs` and `/redoc` no longer repeat the root path (`/prefix/prefix/docs?version=...`) and lead to a 404 (#501).

## [8.1.0]

### Deprecated

- The deprecated changelog (`/changelog`, `generate_changelog()`, and `hidden()`) will be removed in 9.0.
- `.had(alias=...)` and `.didnt_have("alias")` now warn at startup (`FutureWarning`) when the field keeps the `validation_alias` or `serialization_alias` that Pydantic copied from its old alias, and so keeps its old key. Cadwyn 9.0 will change them too (#465).

### Added

- Documented Cadwyn's design decisions, accepted trade-offs, known limitations, and the rules for changing its behavior in [Design decisions and trade-offs](https://docs.cadwyn.dev/theory/design_decisions/) (#202).

### Fixed

- With FastAPI 0.159.0 or newer, endpoints that take a form model (e.g. `body: Annotated[MyModel, Form()]`) no longer fail with a 500 error in every API version.

## [8.0.0]

This release fixes many long-standing bugs, most of them reported by users. Most apps can upgrade without changes. It is a major release because some fixes change wrong behavior that apps or their clients may rely on. Before upgrading, check the **Breaking** entries and the **Upgrade note** below. In short:

- Cadwyn now requires Pydantic 2.11.5 or newer.
- Older API versions use their own versions of the models inside generic models such as `Page[Item]`.
- Older API versions of endpoints with postponed annotations validate request bodies with their own models.
- `schema(Child).field(...)` instructions for inherited fields no longer leak into parent and sibling models.
- `Annotated[T, Depends(f)]` dependencies use the versions of models and enums that the request's API version has, in the versions that change them.

### Added

- `schema(Model).had(description=...)` gives older versions of a model their own OpenAPI description (#168).
- Documented how to serve endpoints to requests without an API version, e.g. the OAuth2 token endpoint (#230).
- Documented how to use fastapi-pagination with Cadwyn (#191).
- Documented that request and response migrations only run for the schema that the route uses directly, not for that schema nested in other schemas.
- Documented that computed fields that return versioned models keep the latest versions of these models in older API versions.

### Changed

- **Breaking:** Cadwyn now requires Pydantic 2.11.5 or newer.
- **Breaking:** generic models such as `Page[Item]` now use the versioned copies of their type args in requests, responses, and OpenAPI (#416, #420). Older versions validate and serialize these args with their own models, so older clients that relied on the latest models inside generics may need converters for the generics: converters for `Item` don't run for the items of `Page[Item]`. The OpenAPI names of some generics change, and `migrate_response_body` no longer accepts instances of the latest models inside them. See [Generic models](https://docs.cadwyn.dev/concepts/schema_generation/#generic-models) for details.
- **Breaking:** `schema(Child).field(...).had(...)` and `.didnt_have(...)` for an inherited field now change it only in `Child` and its subclasses, not in its parent and siblings (#466). To keep the old behavior, give the instruction to the parent instead. If a parent renames such a field, the subclass's changes follow the new name only through single, non-generic inheritance. `cadwyn render` now renders each model's own attributes in each version. See [Fields that a schema inherits](https://docs.cadwyn.dev/concepts/schema_migrations/#fields-that-a-schema-inherits) for diamonds and generics.
- **Breaking:** dependencies declared as `Annotated[T, Depends(f)]`, `Annotated[T, Security(f)]`, or `Annotated[f, Depends()]` now use the version's own copy of `f` in the versions that change the models or enums its parameters validate (#467). Requests that relied on the latest dependency can now get a 422, and in these versions the dependency receives the version's copies of models and enums, so compare them by value rather than with `isinstance` or `is`. See [Versioned dependencies](https://docs.cadwyn.dev/concepts/version_changes/#versioned-dependencies).
- Self-referencing models that don't have, refer to, or pass on any instructions are no longer copied for every version: `generate_versioned_models(versions)[version][Category] is Category`.
- `app.dependency_overrides` is now FastAPI's plain attribute. Removed the undocumented `cadwyn.applications.FakeDependencyOverridesProvider` and the `Cadwyn.dependency_overrides` property.
- Type checkers now accept assigning an `APIRouter` to `app.router`, as in FastAPI. Cadwyn still needs its own router there, so don't replace it.

### Deprecated

- The `openapi_prefix` argument of `Cadwyn()`. FastAPI 0.145.0 removed it, so on FastAPI 0.145.0 and newer it now raises a clear `TypeError`, and on older FastAPI it emits a `DeprecationWarning`. Configure your server's root path instead.

### Fixed

- **Upgrade note:** older API versions of endpoints with postponed (`from __future__ import annotations`) or quoted request body annotations used the latest models since 6.0.1. Now they use their own models, as documented, so clients that kept sending the latest shape may get a 422.
- Fixed apps failing to start, or every request failing with a 500 (`RecursionError`), when models refer to themselves, e.g. `children: "list[Category]"` (#419). Where Cadwyn can't copy such models, it now raises a `SchemaGenerationError` that names them. See [Models that refer to each other](https://docs.cadwyn.dev/concepts/schema_generation/#models-that-refer-to-each-other).
- Fixed responses failing with a 500, and request bodies failing in non-head versions, when a required field validates a different key than it serializes to, e.g. `validation_alias` or `serialization_alias` (#260, #370). Bodies that worked before are unchanged.
- Fixed OpenAPI documenting the API version header as optional when `api_version_default_value` isn't set (#277).
- Fixed older API versions adding `"example": {}` to the OpenAPI of parameters declared with defaults such as `= Query()` (#417).
- Fixed `Cadwyn()` failing on FastAPI 0.145.0 and newer (#462).
- Fixed requests without a body failing with a 500 on FastAPI 0.151.0 and newer when the endpoint or its dependencies have embedded optional body params, a `None` default that their type rejects, or a quoted body annotation.
- Fixed apps served with `uvicorn module:app` returning a 500 for every request since 7.1.1.
- Fixed `Cadwyn(strict_content_type=False)` having no effect.
- Fixed uploaded files declared as `bytes`, in endpoints and dependencies, arriving empty in non-head versions. These versions now hold such uploads in memory twice per request. Dependencies that move an upload only when `current_dependency_solver` is `"fastapi"` may now see it moved back to its start; see [Dependency re-execution warning](https://docs.cadwyn.dev/concepts/version_changes/#dependency-re-execution-warning).
- Fixed embedded request bodies failing in non-head versions when the endpoint has a parameter named `body` or a body alias that matches another parameter's name.
- Fixed enums without enum instructions being copied for every version, which broke strict models.
- Fixed versioned dependencies:
    - `functools.partial` dependencies lost their annotations;
    - dependencies described only by `__signature__` lost their parameter types;
    - async generator dependencies didn't receive exceptions raised after `yield`;
    - `Depends(scope=...)` was lost;
    - `app.dependency_overrides` was ignored in older API versions for class instances, bound methods, and partials;
    - parameter defaults such as `= Query()` were shared between versions and apps.
- Fixed startup failures for:
    - `functools.lru_cache` dependencies with postponed annotations;
    - dependency annotations that use names imported under `TYPE_CHECKING` on Python 3.14;
    - dependency return annotations that only type checkers can evaluate (Cadwyn no longer versions them, like FastAPI).
- Fixed older API versions failing on models that use:
    - `typing.Self`;
    - annotations that can't be rebuilt, such as a bare `Callable` or `Unpack[Ts]`;
    - default factories without a signature;
    - names imported under `TYPE_CHECKING` on Python 3.14;
    - generic fields that parametrize generics with their own type parameters, e.g. `items: list[Wrapper[T]]`.
- Fixed `TypeError: unhashable type` in older API versions for unhashable request body or response model annotations.
- Fixed `schema(...).field(...).didnt_have(...)` having no effect on `title`, `description`, `examples`, and `deprecated` declared through `Annotated[..., Field(...)]`.
- Fixed `didnt_have("title")`, `didnt_have("min_length")`, and `didnt_have(...)` failing with `TypeError` on `Annotated` types with validators, `Len`, `Strict`, or `AllowInfNan`.
- Fixed `had(default=...)` and `had(default_factory=...)` failing with pydantic's `TypeError` at startup when they leave a field with both a default and a default factory. Cadwyn now raises `InvalidGenerationInstructionError`, and the instruction's value replaces the one from `Annotated[..., Field(...)]`.
- Fixed the deprecated `/changelog` failing with a 500 for self-referencing models, and on every call after the first one for instructions on inherited fields.
- Fixed type checking with Pydantic 2.14 and ty 0.0.86.

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

- `latest` is now named `head` because it no longer represents the newest version. Instead, it is the `internally used` version and the version that is used for generating all other versions.
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

## [8.1.0]

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
