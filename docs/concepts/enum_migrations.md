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

Cadwyn only knows the values of the members, not the arguments that they were defined with. So an enum with a custom `__new__` that gets more than the value (e.g. `earth = (1, 5.97)` for `__new__(cls, value, mass)`) can't be copied, and Cadwyn raises a `SchemaGenerationError` at startup. Compute such attributes from the value instead, e.g. with a property:

```python
{! ./docs_src/concepts/enum_migrations/block003.py !}
```
