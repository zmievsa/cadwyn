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

Unchanged members also keep attributes assigned after class creation or by a metaclass, including slots. Replaced members and aliases that become canonical members are constructed for their version instead of inheriting the old member's state.

Enum-valued members keep the inner enum member as their value, and `had()` also accepts these values as written. Pydantic serializes them to the inner member's JSON value and documents that value in OpenAPI, as it does for the original enum. If the inner enum is a plain `Enum`, Pydantic rejects its raw value (e.g. `"a"` for `Inner.a`): return the outer member from endpoints instead. To keep accepting raw values, define the outer enum and its `had()` instructions with the inner members' `.value` explicitly.

`had(member=auto())` uses the enum's `_generate_next_value_`; use explicit values to preserve values from an earlier Cadwyn release. Plain enums and flags accept latest-version members with matching values (including flag combinations with no unknown bits), before trying `_missing_`. Request migrations still see the client version's members; endpoints get the latest members, so compare union fields to members rather than raw values.

Cadwyn only knows the values of the members, not the arguments that they were defined with. So an enum with a custom `__new__` that sets attributes from more than the value (e.g. `mass` from `earth = (1, 5.97)` for `__new__(cls, value, mass)`) can't be copied, and Cadwyn raises a `SchemaGenerationError` at startup. Overwriting attributes that its constructor sets also fails this check. Compute such attributes from the value instead, e.g. with a property:

```python
{! ./docs_src/concepts/enum_migrations/block003.py !}
```
