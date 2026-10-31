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
