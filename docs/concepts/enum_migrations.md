# Enum migrations

The instructions below affect only OpenAPI schemas and their initial validation. All of your incoming requests will still be converted to your HEAD schemas.

## Adding enum members

Note that adding `enum` members **can** be a breaking change in contrast to adding optional fields to a schema. For example, if you return a list of entities, each of which has some type, and you add a new type, then your clients' code is likely to break.

It is recommended to add `enum` members in new versions as well.

```python
{! ./docs_src/concepts/enum_migrations/block001.py !}
```

## Removing enum members

```python
{! ./docs_src/concepts/enum_migrations/block002.py !}
```

## How Cadwyn copies enums

For every version, Cadwyn copies the enums that have `enum(...)` instructions. A copy behaves like the enum that you would write by hand for that version: it keeps aliases, `nonmember()` attributes, and `@member` functions, and `had()` takes the same values that you would write in the class body.

Copies retain constructor-produced state for their version, even when construction deliberately leaves an attribute unset. Unchanged members keep extra attributes and slots assigned outside construction, such as labels added after class creation or by a metaclass. Replaced members and aliases that become canonical members are constructed for their version instead of inheriting the old member's state. Cadwyn runs your enum's `__new__` and `__init__` again while it copies the enum, possibly several times per version, so don't rely on side effects of construction such as a global counter.

Known limitation: enum-valued members are unwrapped in generated copies, including values passed to `had()`. For example, if `Outer.a = Inner.a`, the copy stores `Inner.a.value` rather than `Inner.a`; members whose raw values compare equal can become aliases. This behavior is retained to preserve existing requests, responses, validation errors, and OpenAPI. Use raw values directly when defining versioned enums.

Cadwyn does not replay a metaclass declared only on the enum itself: metaclasses can require class-creation arguments that the enum has not stored. Members added with `had()` and aliases that become canonical members can therefore lack attributes assigned only by the metaclass. Use a property or member constructor for attributes that these members need.

`had(member=auto())` uses the enum's `_generate_next_value_`; use explicit values to preserve values from an earlier Cadwyn release. Plain enums and flags accept latest-version members with matching values (including flag combinations with no unknown bits), before trying `_missing_`. Request migrations still see the client version's members; endpoints get the latest members, so compare union fields to members rather than raw values.

Cadwyn only knows the values of the members, not the arguments that they were defined with. So an enum with a custom `__new__` that sets attributes from more than the value (e.g. `mass` from `earth = (1, 5.97)` for `__new__(cls, value, mass)`) can't be copied, and Cadwyn raises a `SchemaGenerationError` at startup. Overwriting attributes that its constructor sets also fails this check. Compute such attributes from the value instead, e.g. with a property:

```python
{! ./docs_src/concepts/enum_migrations/block003.py !}
```

Plain enums and flags with `enum(...)` instructions can be used in supported lazy request fields such as `Iterable[Kind]` and `Generator[Kind, None, None]`. Cadwyn converts their members to the latest enum as the endpoint consumes them; unconsumed items stay unvalidated. Request migrations still see the request version's members. Conversion leaves unions with alternative branches and opaque validation paths unchanged; see [known limitations](../theory/design_decisions.md#known-limitations).

Strict response fields of these plain enums and flags, including fields inside nested models and containers, accept the latest enum's members when their values match a member of the client's version exactly. Strict validation still rejects raw values and members of unrelated enums. A member that the older version doesn't have needs a response migration as usual.

Unions with alternative branches keep their existing validation and serialization behavior. In a version that uses an enum through such a union, that enum's strict fields can still reject latest members, including fields outside the union: Cadwyn shares one enum copy per version. `Optional[Kind]` alone has no alternative value branch and supports strict validation. Core-schema hooks are opaque, including hooks in `Annotated` metadata (`GetPydanticSchema`, `PlainValidator`, `WrapValidator`, and similar) or on wrapper types, as well as decorator validators. A hook can introduce enums absent from its annotations, so all enum copies in that API version keep legacy validation. Enums with class-level `__get_pydantic_json_schema__` hooks, and enums based on `str`, `int`, or `float`, also retain their previous strict behavior. Use `Field(strict=False)` on affected response fields or a response migration that supplies the client's own members.
