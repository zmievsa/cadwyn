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

Copies retain the state that construction computes for their version, including attributes such as a member's position in the enum. Unchanged members also keep missing attributes and slots assigned after class creation or by a metaclass. Replaced members and aliases that become canonical members are constructed for their version instead of inheriting the old member's state.

Enum-valued members keep the inner enum member as their value, and `had()` also accepts these values as written. Generated copies still accept the inner members' raw values in requests and responses, so JSON serialization and OpenAPI values stay the same.

Cadwyn does not replay a metaclass declared only on the enum itself: metaclasses can require class-creation arguments that the enum has not stored. Members added with `had()` and aliases that become canonical members can therefore lack attributes assigned only by the metaclass. Use a property or member constructor for attributes that these members need.

`had(member=auto())` uses the enum's `_generate_next_value_`; use explicit values to preserve values from an earlier Cadwyn release. Plain enums and flags accept latest-version members with matching values (including flag combinations with no unknown bits), before trying `_missing_`. Request migrations still see the client version's members; endpoints get the latest members, so compare union fields to members rather than raw values.

Cadwyn only knows the values of the members, not the arguments that they were defined with. So an enum with a custom `__new__` that sets attributes from more than the value (e.g. `mass` from `earth = (1, 5.97)` for `__new__(cls, value, mass)`) can't be copied, and Cadwyn raises a `SchemaGenerationError` at startup. Overwriting attributes that its constructor sets also fails this check. Compute such attributes from the value instead, e.g. with a property:

```python
{! ./docs_src/concepts/enum_migrations/block003.py !}
```
