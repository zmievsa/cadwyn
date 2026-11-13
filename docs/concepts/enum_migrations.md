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

The members of a copy get the state that your enum's constructor gives them in that version, even when it leaves an attribute unset. Unchanged members also keep the attributes and slots that you set, overwrite, or delete after construction, such as labels added after class creation or by a metaclass. Cadwyn finds them by building the HEAD enum again from the values of its members and comparing the result with your members: wherever they differ, unchanged members keep the state of your HEAD member. If the constructor makes a different value on every call, such as a new object, Cadwyn can't tell whether you overwrote it, so the copy keeps what its constructor makes. Replaced members and aliases that become canonical members are constructed for their version instead of inheriting the old member's state. Cadwyn runs your enum's `__new__` and `__init__` again while it copies the enum, once or twice for the HEAD enum and once more per version, so don't rely on side effects of construction such as a global counter.

Cadwyn does not replay a metaclass declared only on the enum itself: metaclasses can require class-creation arguments that the enum has not stored. Members added with `had()` and aliases that become canonical members can therefore lack attributes assigned only by the metaclass. Use a property or member constructor for attributes that these members need.

`had(member=auto())` uses the enum's `_generate_next_value_`; use explicit values to preserve values from an earlier Cadwyn release. Plain enums and flags accept HEAD-version members with matching values (including flag combinations with no unknown bits), before trying `_missing_`. Request migrations still see the client version's members; endpoints get the HEAD members, so compare union fields to members rather than raw values. Lazy fields such as `Iterable[Kind]` get the HEAD members too, as the endpoint consumes them.

Cadwyn only knows the values of the members, not the arguments that they were defined with. So an enum with a custom `__new__` that sets attributes from more than the value (e.g. `mass` from `earth = (1, 5.97)` for `__new__(cls, value, mass)`) can't be copied, and Cadwyn raises a `SchemaGenerationError` at startup. Overwriting or deleting attributes that `__new__` sets also fails this check, because Cadwyn can't tell them apart from attributes that `__new__` sets from other args. Compute such attributes from the value instead, e.g. with a property:

```python
{! ./docs_src/concepts/enum_migrations/block003.py !}
```

Strict response models still reject the HEAD members of plain enums and flags with `enum(...)` instructions, including in optional fields and nested models or containers. Use `Field(strict=False)` on affected response fields or a response migration that supplies the client's own members; see [known limitations](../theory/design_decisions.md#known-limitations).
