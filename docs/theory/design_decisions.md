# Design decisions and trade-offs

This page lists the trade-offs Cadwyn has accepted, its known limitations, and the rules for changing it. Read it before you propose a change to Cadwyn's behavior.

## Why most fixes are breaking changes

Cadwyn's output for an older API version is a public API. Clients of apps built on Cadwyn depend on it and can't opt out of a Cadwyn upgrade. Any change to what an older version accepts, returns, or documents in OpenAPI can break them, even when the old behavior was a bug. So Cadwyn treats the OpenAPI and behavior of every version as a frozen contract, and fixing long-standing bugs made 8.0.0 a major release.

The maintainer's principles follow from this:

1. Existing users should almost never get breaking changes.
2. FastAPI and Pydantic features should keep working transparently, but only as far as FastAPI and Pydantic themselves go.
3. Every change should make Cadwyn simpler if possible. Complexity is the main long-term cost: after 8.0.0, `schema_generation.py` was about 4,000 lines and `structure/versions.py` about 2,000.

Cadwyn doesn't aim for "pay only for what you use". Prefer one behavior and one code path for every app over special paths that only some apps take, so bugs show up for everyone and get fixed once.

## Supported API

Everything importable from `cadwyn` is supported. So are these internals, which Airflow's Task SDK builds on. Changing them is a breaking change:

* `VersionBundle.versions` and `VersionBundle.reversed_versions`
* the `alter_*_instructions` attributes of `VersionChange`
* calling the instructions in them (`_AlterDataInstruction.__call__`) with any object that has a `body` attribute
* `generate_versioned_models()`, and indexing its result (`SchemaGenerator.__getitem__` from `cadwyn.schema_generation`)

## Costs of the architecture

These follow from how Cadwyn works (see [Architecture](architecture.md)). They aren't bugs, and changes that try to remove them are almost always rejected.

* Business logic sees only the latest version. Older versions are generated copies of the latest models, and migrations are written against the latest models.
* Older versions use copies of the models and of changed enums. Identity checks (`isinstance`, `is`, dicts keyed by enum members) see these copies, e.g. in the first run of a versioned dependency. Objects that can't be deep-copied, such as a validator holding a `threading.Lock`, fail when their models are versioned.
* Requests are validated twice: by their version's route, then, after migration, by the latest version's route. Dependencies run in both passes, so their side effects happen twice unless they check `current_dependency_solver`. Uploaded `bytes` files are held in memory twice. A model object that an endpoint returns is validated again against Cadwyn's copy of the response model, so its validators and `model_post_init` run twice, even with a single version (#592). Skipping that would also skip the coercion of objects built with `model_construct()` or `model_copy(update=...)`, and change the responses of apps that rely on it.
* Migrations run only for the schema you name: the route's body or `response_model`, or a member of a union that is (for the requests and responses that are that member). They don't run for that schema nested in other schemas or generics. Schema instructions change a schema everywhere it's used. This way each migration runs once, where it was declared.
* Cadwyn relies on FastAPI, Pydantic, and `typing` internals, such as `solve_dependencies`, `fastapi._compat`, FastAPI's lazily included routers, `FieldInfo` internals, and `typing`'s alias cache, so upstream minor releases can break it. The twice-daily workflow upgrades every dependency and runs the tests to catch this. It must always pass. Fix failures in Cadwyn, never by pinning or capping a dependency.
* Startup copies every model for every version. Copying only the changed models would be much faster, but it would change OpenAPI names and model behavior, so it isn't planned.

## Decisions already made

| Area | Decision | Why |
| --- | --- | --- |
| Generic models (`Page[Item]`) | Their type args are always versioned, with no option to turn it off. | User-reported bug (#416). Options double the test matrix and the code paths. |
| Inherited fields | `schema(Child).field(...)` changes only `Child` and its subclasses. | User-reported bug (#466). |
| Renaming a field in a subclass while a base still has it | Fails at startup, naming the base. Rename the field where it's declared, and in the same version in subclasses that changed it in newer versions. | The subclass had both fields, because Pydantic models can't drop inherited fields. Not inheriting from the parent's copy would break the parent's validators and `isinstance` (#525). |
| Renaming a field in a parent while a subclass declares it, changed it in a newer version, or gets it from another base | Fails at startup, naming the subclass. Rename it in the subclass or the other base too, in the same version. | The subclass had both fields. Renaming it automatically would break apps that already rename it in both, and would guess when other bases have the new name (#581). |
| `didnt_exist` for a field that a subclass declares again or changed, while a base still has it | Fails at startup, naming the base. Delete it from the base in the same version. | The instruction did nothing, without an error, because the subclass still inherited the base's field (#582). |
| `Annotated[T, Depends(f)]` | Versioned only in the versions that change the models or enums that `f` validates. | Versioning it everywhere exposed copies to identity checks (#467). Missing an exotic case, such as a recursive type alias, keeps the previous behavior, which is acceptable. |
| Converters of union members (`Union[A, B]`, `Optional[A]`) | Since 9.0, they run for the member that the client's version validated, or whose model the endpoint returned. If Cadwyn can't tell the member, it fails. A migration of the whole union replaces its members' migrations in the same version change. | User-reported bug (#530). Before 9.0, they never ran for unions. Running one member's migration on another's data would corrupt values. Validating responses to find the member would run their validators twice, since FastAPI validates them after the migrations. |
| Converters inside nested models | Won't do. Write the converter for the outer schema. | Running nested converters by default would convert data twice in apps that already follow the docs (#468). |
| Computed fields that return versioned models | Documented limitation. Expose the data as a regular field. | Every Pydantic serializer that picks a model by the value's type loses nested JSON config, changes duck-typed values, or re-wraps user exceptions (#469). |
| `.had(alias=...)` and `.didnt_have("alias")` | Since 9.0, they also change the validation and serialization aliases that Pydantic derived from `alias`, as `Field()` does, so the wire key matches the documented one. Data with only the old key fails (a 422 or a 500) instead of getting the field's default. 8.x only warned at startup. | Clients of apps without migrations relied on the old key, so the fix waited for a major release and must fail loudly instead of losing their values (#465). |
| Fields whose validation and serialization keys differ | Fix only the requests and responses that would certainly fail ("rescue-only"). | Responses that passed before must not change, even if they lost a value. |
| Self-referencing models | Copied when that's safe. Otherwise, Cadwyn raises `SchemaGenerationError` at startup. | A clear failure is better than serving wrong shapes. |
| Enum defaults of omitted parameters on FastAPI before 0.148 | Not rewritten. | It would fix a pre-existing bug on old FastAPI only, and every attempt added new edge cases. |
| `/changelog`, `generate_changelog()`, and `hidden()` | Removed in 9.0. | Maintenance cost. Apps can generate a better changelog from their version changes themselves, e.g. with a coding agent. |
| Options and flags | Avoided in 99% of cases, and added only with the maintainer's sign-off. A breaking fix doesn't get one to ship early: it waits for a major release. | Each one doubles the code paths and the tests. |
| Minimum supported versions | Raising the FastAPI minimum to drop compatibility code is fine. The Pydantic and Python minimums are raised only when unavoidable. | FastAPI moves fast, and Cadwyn depends on its internals. Raising the Pydantic or Python minimum blocks more users. |
| FastAPI before 0.137.1 | Not supported. | Its router model is different. Supporting both doubles the routing code. |

## Known limitations

These are accepted for now. Fixing one is a separate change with its own issue.

* A generic model used without type args (e.g. `Page` instead of `Page[Item]`) fails at startup with a `SchemaGenerationError` if its typed extra fields (`__pydantic_extra__` with `extra="allow"`) use a type parameter whose bound, constraints, or default change in a version, because Pydantic versions differ in how they substitute parameters there. Use type args, or a concrete annotation such as `__pydantic_extra__: dict[str, Item]` ([#584](https://github.com/zmievsa/cadwyn/issues/584)).
* Generic models keep the latest models in older versions when a type arg refers back to its own model through a quoted annotation, such as `Optional[Page["Category"]]` in `Category`, or uses a recursive native `type` alias. A PEP 695 generic model that refers to itself through a quoted annotation, such as `nxt: Optional["Wrap[X]"]` in `class Wrap[X](BaseModel)`, returns a 500 in every version instead of failing at startup. On Pydantic 2.14.2 and newer, this happens only when a version change alters its type arg.
* Recursive native `type` aliases work only if they contain nothing but builtin types (including `Union` and `Literal`) and other such aliases, like `type JsonData = dict[str, JsonData] | list[JsonData] | str | int | None`. A route that uses one with anything else in it, such as a model, an enum, a forward ref, or `Annotated`, fails with a `SchemaGenerationError` that names the alias when Cadwyn generates the route. Models in a reference cycle (e.g. `children: "list[Category]"` in `Category`) that use such an alias fail with a `SchemaGenerationError` if any version has schema or enum instructions.
* Enum copies unwrap enum-valued members, including values passed to `had()`. For `Outer.a = Inner.a`, the copy's value is `Inner.a.value`, and members with equal raw values can become aliases. Changing this would change existing requests, responses, validation errors, and OpenAPI. Define versioned enums with raw values (see [Enum migrations](../concepts/enum_migrations.md#how-cadwyn-copies-enums)).
* A request with a plain `Enum` or `Flag` that has `enum(...)` instructions returns a 500 if Cadwyn can't convert its members to the latest enum. Cadwyn converts them in standard containers and in plain lazy fields such as `Iterable[Kind]`. It doesn't convert them in models it doesn't dump (e.g. a top-level `Iterable[Item]` body), in your own containers or iterators, in embedded bodies, with dependency overrides, or in lazy fields with alternative union branches, `Any`, opaque validators, complex validation aliases, or enums with custom lookup, equality, or non-primitive values.
* A strict response model (e.g. `ConfigDict(strict=True)`) with a plain `Enum` or `Flag` that has `enum(...)` instructions returns a 500 in every version, including the latest. This includes optional fields and nested models or containers. Add `Field(strict=False)` to the enum field. In older versions, a response migration that supplies that version's enum members also works (see [Enum migrations](../concepts/enum_migrations.md#how-cadwyn-copies-enums)). A fix would change the enum's schema and what custom schema hooks see.
* Schema instructions:
    * Diamond inheritance takes the field from the first base, as Pydantic does.
    * A parent's rename reaches a subclass's own changes only through single, non-generic inheritance.
    * A parent's rename of `x` to `y` leaves both `x` and `y`, without a startup error, in a subclass that declares `x` again, if a class between them changed `x` in a newer version and deletes it in the version of the rename. For example, `schema(Middle).field("x").had(description=...)`, then `schema(Parent).field("x").had(name="y")` and `schema(Middle).field("x").didnt_exist` with `class Child(Middle)` declaring `x`.
    * `had(default=PydanticUndefined)` doesn't make a field required when its default is declared in `Annotated[..., Field(default=...)]`.
    * `didnt_have(...)` raises an `AttributeError` when the attribute is a read-only property of an `Annotated` metadata object.
    * `schema(Model).validator(...).existed` for a computed field fails on Python before 3.13.
* Alias instructions decide from the incoming data whether it has only a field's old key (see `.had(alias=...)` in [Decisions already made](#decisions-already-made)). So the `renamed_key` error happens even when a model validator would fill the field from other keys, e.g. a `model_validator(mode="before")` that copies a `compatibility_name` key into it.
* Defaults: Cadwyn swaps in the version's model classes, enum classes, and functions only inside the lists, tuples, and dicts in defaults and other field attributes (see [Defaults](../concepts/schema_generation.md#defaults)). Elsewhere, they stay the latest ones: e.g. in sets, deques, `MappingProxyType`, a `defaultdict`'s `default_factory`, dataclass and model instances, or `functools.partial` args. Each version gets a `copy.deepcopy()` of subclasses of `list`, `tuple`, and `dict`, and of containers that contain themselves. So a custom `__deepcopy__` or `__reduce__` decides what versions share (one that returns `self` shares the whole default), a sentinel inside is a different object in every version, `Counter` loses its attributes, and some cycles through tuples lose identity.
* `cadwyn render` renders `model_config` only for models whose config a version changes, and omits it for the rest. It renders `AliasChoices` and `AliasPath` as calls that raise a `TypeError`, and subclasses of scalars such as `int` and `str` as the base type. It raises `CadwynRenderError` for defaults it can't reproduce, such as recursive or shared mutable defaults, subclasses whose `repr` looks like a builtin container or whose constructor changes the items it's given, distinct set items or dict keys with the same `repr`, or a `defaultdict` with a lambda factory. Use a named `Field(default_factory=...)` function for these (see [Rendering schemas](../concepts/schema_generation.md#rendering-schemas)). Other subclasses render through their constructor and `__repr__`, which must reproduce their contents and state.
* Class dependencies (except Pydantic models) aren't versioned.
* WebSocket routes select the API version and support existence, path, name, and dependency instructions, but Cadwyn doesn't migrate their schemas or messages.
* Streaming (generator or server-sent-event) endpoints don't work in versioned routes.
* Request bodies that are lists or dicts of models (e.g. `list[Model]`) pass the client's extra keys and set fields to the latest version only when Cadwyn can be sure they're right ([#566](https://github.com/zmievsa/cadwyn/issues/566)). Roughly, both versions must have the same fields, keys, and defaults at the same place, directly in lists, dicts, or unions, with no validators that get the raw input (e.g. `model_validator(mode="before")`) and no custom `__init__`. Every latest-version param that reads the body must validate it the same way. Models in tuples, sets, or deques, root models, and all bodies in apps with `app.dependency_overrides` never qualify. Other models are read by their attributes, as before: they lose their extra keys, and all their fields count as set. A required field whose alias differs from its name returns a 500. An optional one whose model doesn't validate its serialization alias (e.g. `Field(default=0, alias="x", validation_alias="v")`) gets its default without an error.
* Request body models that override `model_dump()` (e.g. SQLModel models) still pass their computed fields to the latest version as input: as extra keys, or as a 500 if the model forbids extra keys. Cadwyn can't tell whether an override accepts `exclude_computed_fields` (SQLModel before 0.0.26 doesn't), so it calls it as before (#570).
* `Json[...]` fields of request bodies ([#538](https://github.com/zmievsa/cadwyn/issues/538)): Cadwyn encodes their values for the latest version only when it can be sure that the latest version gets what the client sent. Otherwise, the latest version parses the already parsed values again. A value that isn't a string then returns a 500 in every version, unless a migration encoded it as a JSON string. `Json[str]` returns a 500 or another string. `Json[Any]` and other types that accept several JSON types give the endpoint another value, e.g. `123` instead of `"123"`. Cadwyn encodes only the strings that the client sent and that are still at their place, including in copies made with `dict()` or `copy.deepcopy()`. It passes values as they are when:
    * a migration set the value as a string, or the value doesn't survive a JSON round trip (e.g. an empty string, null, a tuple, a NaN, or a dict with mixed key types). Cadwyn still encodes the client's other strings. One exception: if a migration interns the client's string with `sys.intern()` and puts equal interned JSON at the same place, Python returns the client's string object. Cadwyn then encodes it, and the endpoint gets a string instead of the parsed JSON.
    * a migration moved or removed a client's string (e.g. by reordering a list), replaced a list, dict, or model with another type, or put keys other than strings, numbers, booleans, and null in a dict on the way to the field
    * the body differs between the request's version and the latest one, or a latest-version dependency reads it as another type (e.g. `body: dict = Body()`)
    * the body's schema isn't plain JSON, e.g. it has validators, serializers, `model_post_init`, computed fields, default factories other than builtin containers (e.g. `Field(default_factory=lambda: 1)` next to a `Json[...]` field returns a 500), unions with non-JSON members (e.g. models or dates), models that allow extra keys or dump to other keys, or `Json[...]` fields in tuples, in self-referencing models, or more than 8 models deep
    * the request has no body, or, on FastAPI 0.158.2 and newer, a migration sets the body of an omitted optional form model
    * the field is in a `list` or `dict` body that falls back (see above), or has a string value there
    * a request migration runs and the field has a string that Python shares, such as `""` or some one-character strings (e.g. `"5"` with some pydantic-core versions, which becomes `5`)
* Unions of form models on FastAPI 0.153.0 and newer: Cadwyn can't pass a list, set, or tuple from a validator or a migration whole to every member when one member takes that field as a single value (e.g. `value: Any`) and another as repeated values (e.g. `value: list[str]`) or as an extra field. On FastAPI 0.153.0 to 0.160.x, the body passes as it is, like in Cadwyn 8.x: a single-value field gets only the last item, and a repeated-values field gets a set or tuple as one item. If that member accepts the body, the endpoint gets a wrong value without an error, e.g. `["b"]` instead of `["a", "b"]`. On FastAPI 0.161.0 and newer, the request fails with a `CadwynError` that names the field and the members, as it does when a form model outside the union (e.g. of a dependency override) shares the body and would get a list in a list.
* Form models on FastAPI 0.158.2 and newer: FastAPI reads a form model and the other form params of the endpoint, its dependencies, and their overrides from one dict body. Cadwyn gives each param what FastAPI would give it for the migrated request, e.g. an `int` where the form had a string, if a migration set one.
    * If Cadwyn can't, the request fails with a `CadwynError` (a 500): when a param takes the whole body (e.g. `payload: dict = Body()` in an override), when another param uses the alias of an endpoint's form model that a dependency declares, or, after a migration, when a param reads a key that the form model took under another name (e.g. through `AliasChoices`) or the model's migrated body isn't a dict, and when a migration sets a key to a dict or a collection in a collection while an optional form model is omitted, since a form can't hold it. Use distinct names or aliases, or read the form from `Request`.
    * If an optional form model was omitted and nothing else reads the form, a migrated key named like the model param counts as the embedded model. For `body: Annotated[Optional[MyModel], Form()] = None` whose model has a `body: str` field, `request.body = {"body": "value"}` fails. Set `request.body = {"body": {"body": "value"}}`.
* Cadwyn closes uploaded files after the response, even after the client disconnects. A file close that never returns blocks the request and server shutdown. Plain FastAPI apps mounted in a Cadwyn app close their own uploads, and `VersionPickingMiddleware` interrupts that when the client disconnects, so their files stay open.

## Rules for changing Cadwyn

**Scope**

* Fix the reported bug, not its neighbors.
* File the bugs you find yourself as issues. Fix them only if a user reported them or they crash.
* Don't add unsupported features as part of a fix, such as migrations of WebSocket messages, streaming endpoints, `StreamingResponse`, or `FileResponse`.
* Don't widen a change during review. If a review uncovers a new problem, file it.
* If user code has a simple workaround and the fix in Cadwyn is complex, document the workaround instead.
* Rank review findings by how likely they are in real apps. A contrived case that breaks working output still gets fixed, but it shouldn't drive the design.
* Decide which breaking changes a major release takes when the work starts, not in the audit before the release. 8.0.0 rolled back and redesigned two fixes late (#457 and #475, #458 and #474).

**What a change may alter**

* Compare every change against the last release, not against an idea of correct behavior.
* A change may alter output that is broken today: a crash, a 500, an empty or lost value. It may not alter output that works today, even if it's wrong, unless the maintainer accepts it as a breaking change with an upgrade note in the CHANGELOG.
* The OpenAPI of every version counts as output, including schema names.
* When clients rely on behavior that differs from the docs, a fix toward the docs is a breaking change. Warn at startup in the current major version, naming the model, the field, and how to keep working, and change the behavior in the next one.

**How to fix**

* Prefer doing less. Narrow the fix, apply it only when the old path fails ("retry on failure", not an extra validation pass), or go back to the last release's code path.
* If every review round finds new edge cases in the same part of a fix, remove that part instead of patching it again.
* Don't reimplement FastAPI, Pydantic, or CPython logic. Call their functions, and evaluate annotations only where FastAPI itself does.
* Make safety checks allow-lists. Anything unknown takes the old behavior.
* Decide statically, not by the order in which models happen to be generated.
* Find what needs versioning and copy it in one traversal, never two that must agree. Finding must not copy, wrap, or rebuild unchanged models.
* Keep the user's objects as they are, and copy only what FastAPI or Pydantic would mutate. `deepcopy` doesn't isolate things: it goes through `typing`'s caches and breaks objects compared with `is`, such as FastAPI's sentinels.
* Caches may share structure between models or versions, but never user-visible state such as a stateful default factory.
* Invalid instructions or models fail at startup with an error that names the model and the field. Never fail later at request time, and never serve wrong data without an error.
* When you rely on a private FastAPI or Pydantic internal, check at import time that it exists, and fail with a clear error if it changed instead of falling back.
* `Cadwyn()` accepts FastAPI's parameters and passes each of them to every router it creates.
* In the user docs, describe the common case and its workaround. The full list of shapes that a rule covers belongs in the tests.

**How to check a change**

* Compare the requests and the OpenAPI of every version against the last release, installed in its own checkout or virtual environment.
* Run the tests on the minimum supported and the latest Pydantic, and on Python 3.11 and 3.14. Check CI on every operating system before merging: recursion and stack limits differ on Windows and macOS.
* Pytest puts the root of the checkout it runs from first on `sys.path`, so run a test file from the checkout that you want to test.
* After merging `main` into a branch, look for top-level functions that are now defined twice. The later definition wins without a warning.
