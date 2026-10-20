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

These follow from how Cadwyn works and are not bugs. Changes that try to remove them are almost always rejected.

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

* Generic models: cyclic generic args, quoted args from other modules, and recursive native type aliases keep the latest models in older versions.
* Dependencies: class dependencies (except Pydantic models) are not versioned. `Flag` and `IntFlag` parameters fail. WebSocket routes and streaming (generator or server-sent-event) endpoints don't work in versioned routes.
* Schema instructions: diamond inheritance takes the field from the first base, like Pydantic does. A parent's rename reaches a subclass's own changes only through single, non-generic inheritance.
* `cadwyn render` doesn't render `model_config`.
* Bodies sent without a `Content-Type` are parsed as JSON by Cadwyn but ignored by FastAPI.

## Rules for changing Cadwyn

**Scope**

* Fix the reported bug, not its neighbors.
* Bugs you find yourself go into an issue. Fix them only if a user reported them or they crash.
* Features that Cadwyn doesn't support, such as WebSockets, streaming endpoints, and migrations of `StreamingResponse` or `FileResponse`, aren't added as part of a fix.
* Don't widen a change during review. If a review uncovers a new problem, file it.
* Don't add options or flags to keep both behaviors unless the maintainer has signed off on them.
* If a simple workaround exists in user code and the fix in Cadwyn is complex, document the workaround instead.

**What a change may alter**

* Compare every change against the last release, not against an idea of correct behavior.
* A change may alter output that is broken today: a crash, a 500, an empty or lost value. It may not alter output that works today, even if that output is wrong, unless the maintainer accepts it as a breaking change with an upgrade note in the CHANGELOG.
* OpenAPI of every version counts as output, including schema names.
* When behavior differs from the docs and clients rely on the behavior, a fix toward the docs is a breaking change. Warn about it at startup in the current major version, naming the model, the field, and how to keep working, and change the behavior in the next major version.

**How to fix**

* Prefer doing less. Narrow the fix, apply it only when the old path fails ("retry on failure", not an extra validation pass), or go back to the last release's code path.
* Don't reimplement FastAPI, Pydantic, or CPython logic. Call their functions, and evaluate annotations only where FastAPI itself does.
* Make safety checks allow-lists. Anything unknown takes the old behavior.
* Decide statically, not by the order in which models happen to be generated.
* Invalid instructions or models fail at startup with an error that names the model and the field. Never fail later at request time, and never serve wrong data silently.
* When you rely on a private FastAPI or Pydantic internal, check that it exists at import time and fail with a clear error if it changed. Never fall back silently.
* `Cadwyn()` accepts FastAPI's parameters and passes each of them to every router it creates.
