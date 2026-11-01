# Design decisions and trade-offs

This page records the trade-offs that Cadwyn has already accepted, the known limitations it lives with, and the rules for changing it. Read it before proposing a change to Cadwyn's behavior. Most of the hard questions here have already been answered, and the answers are usually "do less".

## Why most fixes are breaking changes

Cadwyn's output for an older API version *is* a public API: the clients of the apps built on Cadwyn depend on it, and they can't opt out of a Cadwyn upgrade. So every change to what an older version accepts, returns, or documents in OpenAPI can break a third party, even when the old behavior was a bug. This is why Cadwyn treats the OpenAPI and behavior of every version as a frozen contract, and why fixing long-standing bugs made 8.0.0 a major release.

The maintainer's three principles follow from this:

1. Existing users should almost never get breaking changes.
2. FastAPI and Pydantic features should keep working transparently, but only as far as FastAPI and Pydantic themselves go.
3. Every change should make Cadwyn simpler if possible. Complexity is the main long-term cost: after 8.0.0, `schema_generation.py` is about 4,000 lines and `structure/versions.py` about 2,000.

Cadwyn also doesn't aim for "pay only for what you use". Every app should run roughly the same Cadwyn: prefer one behavior and one code path for everyone over special paths that only some apps take, so that bugs show up for everyone and get fixed once.

## Supported API

Everything importable from `cadwyn` is supported. So are the internals that Airflow's Task SDK builds on, and changing them counts as a breaking change:

* `VersionBundle.versions` and `VersionBundle.reversed_versions`
* the `alter_*_instructions` attributes of `VersionChange`
* calling the instructions in them (`_AlterDataInstruction.__call__`) with any object that has a `body` attribute
* `generate_versioned_models()`, and indexing its result (`SchemaGenerator.__getitem__` from `cadwyn.schema_generation`)

## Costs of the architecture

These follow from how Cadwyn works, which [Architecture](architecture.md) describes, and are not bugs. Changes that try to remove them are almost always rejected.

* **Business logic sees only the latest version.** Older versions are generated copies of the latest models, and migrations are written against the latest models.
* **Older versions use copies, not the original classes.** Cadwyn copies models and changed enums for every version. Code that compares by identity (`isinstance`, `is`, dicts keyed by enum members) can see these copies, e.g. in the first run of a versioned dependency. Objects that can't be deep-copied (e.g. a validator holding a `threading.Lock`) fail when their models are versioned.
* **Requests are validated twice, and dependencies run twice.** A request is validated by its version's route, migrated, and validated again by the latest version's route. Dependencies run in both passes, so their side effects happen twice unless they check `current_dependency_solver`. Uploaded `bytes` files are held in memory twice.
* **Migrations only run for the schema you name.** Request and response migrations run when their schema is the route's own body or `response_model`, not for that schema nested in other schemas or generics. Schema instructions, on the other hand, change a schema everywhere it's used. This is deliberate: it keeps each migration running exactly once, where it was declared.
* **Cadwyn relies on FastAPI, Pydantic, and `typing` internals.** For example: `solve_dependencies`, `fastapi._compat`, FastAPI's lazily included routers, `FieldInfo` internals, and `typing`'s alias cache. Upstream minor releases can break Cadwyn. The twice-daily workflow upgrades every dependency and runs the tests to catch this. It must always pass, and failures are fixed in Cadwyn, never by pinning or capping a dependency.
* **Startup copies every model for every version.** Copying only changed models would make startup much faster, but it changes OpenAPI names and model behavior. It's too large a change, and it isn't planned.

## Decisions already made

| Area | Decision | Why |
| --- | --- | --- |
| Generic models (`Page[Item]`) | Their type args are always versioned. There is no option to turn it off. | User-reported bug (#416). Options double the test matrix and the code paths. |
| Inherited fields | `schema(Child).field(...)` changes only `Child` and its subclasses. | User-reported bug (#466). |
| `Annotated[T, Depends(f)]` | Versioned only in the versions that change the models or enums `f` validates. | Versioning it everywhere gave copies to identity checks in every version (#467). Missing an exotic case (e.g. a recursive type alias) keeps the previous behavior, which is acceptable. |
| Converters inside nested models | Won't do. Write the converter for the outer schema. | Running nested converters by default would convert data twice for apps that already follow the docs (#468). |
| Computed fields that return versioned models | Documented limitation. Expose the data as a regular field instead. | Every Pydantic serializer that picks a model by the value's type loses nested JSON config, changes duck-typed values, or re-wraps user exceptions (#469). |
| `.had(alias=...)` and `.didnt_have("alias")` | 8.x warns at startup when the instruction leaves the field's validation or serialization alias unchanged. 9.0 changes them too, so the wire key matches the documented one. | Clients of apps without migrations rely on the old key, so fixing it at once would break them (#465). |
| Fields whose validation and serialization keys differ | Fix only requests and responses that would certainly fail ("rescue-only"). | Responses that passed before must not change at all, even if they lost a value. |
| Self-referencing models | Copied when that's safe. Otherwise Cadwyn raises `SchemaGenerationError` at startup. | Failing clearly is better than serving wrong shapes silently. |
| Enum defaults of omitted parameters on FastAPI before 0.148 | Not rewritten. | It would fix a pre-existing bug on old FastAPI only, and every attempt added new edge cases. |
| `/changelog` and `generate_changelog()` | Deprecated, and removed in 9.0. Until then, crash fixes only. | Maintenance cost. Apps can now generate a better and more extensible changelog from their version changes themselves, e.g. with a coding agent. |
| `openapi_prefix` argument of `Cadwyn()` | Deprecated, and kept at least until 2027-01-20. | FastAPI 0.145.0 removed it. |
| Options and flags | Avoided in 99% of cases, including for shipping a breaking fix early. A breaking fix waits for a major release. | Each one doubles the code paths and the tests. |
| Minimum supported versions | Raising the FastAPI minimum to drop compatibility code is fine. The Pydantic and Python minimums are raised only when unavoidable. | FastAPI moves fast and Cadwyn depends on its internals. Raising the Pydantic or Python minimums blocks more users. |
| FastAPI before 0.137.1 | Not supported. | Its router model is different. Supporting both doubles the routing code. |

## Known limitations

These are accepted for now. They are listed so that nobody "discovers" and fixes them as a side quest. A fix for any of them is a separate, deliberate change with its own issue.

* Generic models keep the latest models in older versions when a type arg refers back to its own model through a quoted annotation, such as `Optional[Page["Category"]]` in `Category`, or when it uses a recursive native `type` alias. A PEP 695 generic model that refers to itself through a quoted annotation, such as `nxt: Optional["Wrap[X]"]` in `class Wrap[X](BaseModel)`, returns a 500 in every version, including the latest one, instead of failing at startup. On Pydantic 2.14.2 and newer, this happens only when a version change alters its type arg.
* Type aliases: a response model with a field typed by a recursive native `type` alias, such as `type JsonData = list[JsonData] | Leaf | int`, fails with a `RecursionError`, so every request returns a 500.
* Enums: the head rejects the members of the copy of a plain `Enum` or `Flag` (`class Kind(Enum)`, not `class Kind(str, Enum)` or `class Kind(IntFlag)`) with `enum(...)` instructions, so Cadwyn solves the requests whose bodies have them again with the head members, and their dependencies with `use_cache=False` run once more. On Python 3.10, a request body with a plain `Flag` that has `enum(...)` instructions still returns a 500 in every version, because `Flag` raises a `TypeError` for the members of its copy there.
* Dependencies: class dependencies (except Pydantic models) are not versioned. WebSocket routes and streaming (generator or server-sent-event) endpoints don't work in versioned routes.
* Uploads and forms: an upload parameter named `body` returns a 500 in every version, and so does a form model with a `Json[...]` field.
* Schema instructions: diamond inheritance takes the field from the first base, like Pydantic does. A parent's rename reaches a subclass's own changes only through single, non-generic inheritance. `schema(Child).field("x").had(name="y")` for a field that `Child` inherits leaves both `x` and `y` in the older versions of `Child`. `had(default=PydanticUndefined)` doesn't make a field required when its default is declared in `Annotated[..., Field(default=...)]`. `didnt_have(...)` fails with an `AttributeError` when the attribute is a read-only property of an `Annotated` metadata object. `schema(Model).validator(...).existed` for a computed field fails on Python versions before 3.13.
* `cadwyn render` doesn't render `model_config`, and it renders `AliasChoices` and `AliasPath` as calls that raise a `TypeError`.
* When a request has no `Content-Type` header, Cadwyn parses its body as JSON for an endpoint with several `Body()` params, while FastAPI ignores the body.

## Rules for changing Cadwyn

**Scope**

* Fix the reported bug, not its neighbors.
* Bugs you find yourself go into an issue. Fix them only if a user reported them or they crash.
* Features that Cadwyn doesn't support, such as WebSockets, streaming endpoints, and migrations of `StreamingResponse` or `FileResponse`, aren't added as part of a fix.
* Don't widen a change during review. If a review uncovers a new problem, file it.
* Don't add options or flags to keep both behaviors unless the maintainer has signed off on them.
* If a simple workaround exists in user code and the fix in Cadwyn is complex, document the workaround instead.
* Rank review findings by how likely they are in real apps. A contrived case that breaks working output still gets fixed, but it shouldn't drive the design.
* Decide which breaking changes a major release takes when the work starts, not in the audit before the release. 8.0.0 rolled two fixes back and redesigned them late (#457 and #475, #458 and #474).

**What a change may alter**

* Compare every change against the last release, not against an idea of correct behavior.
* A change may alter output that is broken today: a crash, a 500, an empty or lost value. It may not alter output that works today, even if that output is wrong, unless the maintainer accepts it as a breaking change with an upgrade note in the CHANGELOG.
* OpenAPI of every version counts as output, including schema names.
* When behavior differs from the docs and clients rely on the behavior, a fix toward the docs is a breaking change. Warn about it at startup in the current major version, naming the model, the field, and how to keep working, and change the behavior in the next major version.

**How to fix**

* Prefer doing less. Narrow the fix, apply it only when the old path fails ("retry on failure", not an extra validation pass), or go back to the last release's code path.
* If every review round finds new edge cases in the same part of a fix, remove that part instead of patching it again.
* Don't reimplement FastAPI, Pydantic, or CPython logic. Call their functions, and evaluate annotations only where FastAPI itself does.
* Make safety checks allow-lists. Anything unknown takes the old behavior.
* Decide statically, not by the order in which models happen to be generated.
* Use one traversal to find what needs versioning and to copy it, never two that must agree. Finding must not copy, wrap, or rebuild the models that it leaves unchanged.
* Keep the user's objects as they are, and copy only what FastAPI or Pydantic would mutate. `deepcopy` isn't a way to isolate things: it goes through `typing`'s caches and breaks objects compared with `is`, such as FastAPI's sentinels.
* Caches may share structure but never user-visible state, such as a stateful default factory, between models or versions.
* Invalid instructions or models fail at startup with an error that names the model and the field. Never fail later at request time, and never serve wrong data silently.
* When you rely on a private FastAPI or Pydantic internal, check that it exists at import time and fail with a clear error if it changed. Never fall back silently.
* `Cadwyn()` accepts FastAPI's parameters and passes each of them to every router it creates.
* In the user docs, describe the common case and its workaround. The full list of shapes that a rule covers belongs in the tests.

**How to check a change**

* Run the same requests and OpenAPI of every version against the last release in its own checkout or virtual environment, and compare the results.
* Run the tests on the minimum supported and the latest Pydantic, and on Python 3.10 and 3.14. Check CI on every operating system before merging: recursion and stack limits differ on Windows and macOS.
* `tox -e docs` writes `site/` into the repository root, which shadows the standard `site` module on Python 3.10 when the root is on `PYTHONPATH`. Delete it before comparing runs.
* Pytest puts the root of the checkout it runs from first on `sys.path`, so run a test file from the checkout that you want to test.
* After merging `main` into a branch, look for top-level functions that are now defined twice. The later definition silently wins.
