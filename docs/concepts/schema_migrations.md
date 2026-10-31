# Schema migrations

The following instructions affect only OpenAPI schemas and their initial validation. All your incoming requests will still be converted to your HEAD schemas.

Please note that you only need a migration if it is a breaking change for your users. The scenarios below only describe "what you can do" but not "what you should do". For the "should" part, please refer to the [how-to docs](../how_to/change_openapi_schemas/add_field.md).

## SQLAlchemy and SQLModel classes

Cadwyn leaves SQLAlchemy and SQLModel classes unchanged when generating API versions. This includes SQLModel classes with or without `table=True`. You can use these classes in dependencies, request bodies, and response models where FastAPI supports them, but their schemas remain the same across API versions.

`schema(...)` and the schema-based forms of `convert_request_to_next_version_for(...)` and `convert_response_to_previous_version_for(...)` reject these classes with a `CadwynStructureError`. To version their API representation, define a separate Pydantic `BaseModel`.

## Add a field to the older version

```python
{! ./docs_src/concepts/schema_migrations/block001.py [ln:1-2,7-16] !}
```

## Remove a field from the older version

```python
{! ./docs_src/concepts/schema_migrations/block002.py [ln:1,6-13] !}
```

## Change a field in the older version

The following code sets an attribute of a field, such as a description:

```python
{! ./docs_src/concepts/schema_migrations/block003.py [ln:1,6-13] !}
```

The following code un-sets an attribute of a field, as if it never existed:

```python
{! ./docs_src/concepts/schema_migrations/block004.py [ln:1,6-13] !}
```

**ALIAS WARNING:**

Pydantic copies a field's `alias` into its `validation_alias` and `serialization_alias`. For a field that already has an alias, `.had(alias=...)` and `.didnt_have("alias")` currently change only `alias`, so the older version keeps using the old key in requests, responses, and OpenAPI. Cadwyn 9.0 will change the copied aliases too, so Cadwyn emits a `FutureWarning` for such instructions when it generates the versioned models, and the warning says what to pass. To use the new key now, also change the copied aliases, e.g. `.had(alias="old_foo", validation_alias="old_foo", serialization_alias="old_foo")`, and add request and response migrations for the key. To keep the current key, remove the alias change from the instruction.

Cadwyn generates the versioned models when the app first runs, at startup or on the first request, so with `-W error` or pytest's `filterwarnings = ["error"]` this warning fails the app there. Fix the instruction if you can. Otherwise, filter the warning:

```python
{! ./docs_src/concepts/schema_migrations/block005.py !}
```

**DEFAULTS WARNING:**

If you add `default` or `default_factory` to the old version of a schema, it will not manifest in the code automatically. Instead, you should add the `default` or `default_factory`, and then also add the default value using a request migration.

The reason for such behaviour is the way Cadwyn works with Pydantic and unfortunately this cannot be changed:

Cadwyn:

1. Receives a request for API version `V`
2. Validates the request using the schemas from `V`
3. Marshalls the unmarshalled request body into a raw data structure using `BaseModel.model_dump` with **exclude_unset=True**
4. Passes the request through all request migrations from `V` to `latest`
5. Validates the request using `latest` schemas

The part that causes the aforementioned problem is Cadwyn's use of `exclude_unset=True`. Unfortunately, when Cadwyn uses it, default fields are not set, so `latest` does not receive them. And if latest does not have the same defaults (for example, if the field has no default and is required in latest), then an error will occur. If Cadwyn used `exclude_unset=False`, then `exclude_unset` would lose its purpose for Cadwyn users, so giving it up is impractical. Instead, you should set all extras in step 4 in your request migrations.

### Fields that a schema inherits

`.had(...)` and `.didnt_have(...)` for a field that a schema inherits change the field only in that schema and its subclasses, not in its parents or its siblings:

```python
{! ./docs_src/concepts/schema_migrations/block006.py !}
```

In the older version, `BusinessInvoice.code` has `max_length=5`, while `Invoice.code` and `PersonalInvoice.code` stay unchanged. To change the field in all of these schemas, give the instruction to the parent: `schema(Invoice).field("code").had(max_length=5)`. Renames don't work this way: `had(name=...)` for an inherited field keeps the old field next to the renamed one in that schema, so rename the field in the schema that declares it.

The changes of a parent's field also apply to its subclasses, including the ones that changed the field themselves, with these exceptions:

- A subclass that already has an instruction of its own for the field, in a newer version or earlier in the same version change, keeps the type and the `Annotated` metadata that the field had at that point. So a later `had(type=...)` of a parent, listed after it or in an older version, doesn't change the type of the field in that subclass.
- If the schema that declares the field has no instructions of its own and no request migrations (`convert_request_to_next_version_for(...)`), the changes that a schema between it and the subclass makes, listed after the subclass's change or in an older version, don't reach a subclass that changed the field itself. Any instruction for the declaring schema, e.g. for another field, makes them apply.

The same holds for generic schemas: an instruction for a parametrization such as `schema(Page[Item])` changes `Page[Item]` and its subclasses, but not `Page` or its other parametrizations.

In multiple inheritance, such as `class D(C, B)`, `D` takes each field from its first base that has it, as in pydantic. So for a field that both `C` and `B` have, e.g. from their common parent, the instructions given only for `B` don't apply to `D` unless `D` changes that field itself. Give the instruction to the common parent, or to `D` as well. The instructions for a field that only `B` has, e.g. because `B` declares it, apply to `D` as usual.

## Add a validator to the older version

```python
{! ./docs_src/concepts/schema_migrations/block007.py [ln:1-2,7-21] !}
```

## Remove a validator from the older version

```python
{! ./docs_src/concepts/schema_migrations/block008.py [ln:1,6-13] !}
```

## Rename a schema in the older version

The following code replaces all schema name occurrences with a new one to ensure that the name is different in openapi.json:


```python
{! ./docs_src/concepts/schema_migrations/block009.py [ln:1,6-13] !}
```

## Change the description of a schema in the older version

Pydantic uses the docstring of a schema as its description in openapi.json, and Cadwyn copies it into every version of the schema. Use `had(description=...)` to give the older versions a different description:

```python
{! ./docs_src/concepts/schema_migrations/block010.py !}
```

Pass `description=None` to remove the description from the older versions. Cadwyn rejects blank descriptions such as `" "`. You can also pass `name` and `description` together, e.g. `schema(User).had(name="OldUser", description="An old user.")`.

Like `name`, the description only applies to the schema itself, not to its subclasses: like in pydantic, every schema has its own docstring. For the same reason, the description of a generic schema such as `Page` does not show up for its parametrizations such as `Page[Item]`, which have no description in pydantic. Like other instructions, the description of a model in the type args of a generic, such as `Item` in `Page[Item]`, also changes there (see [Generic models](./schema_generation.md#generic-models)). If the schema sets `json_schema_extra={"description": ...}` in its `model_config`, that description overrides the docstring in every version.

The latest version keeps the docstring from your code, so if the docstring contains internal notes that no version should show, put the public description into [`HeadVersion`](./version_changes.md#headversion), which applies it to every version, including the latest one:

```python
{! ./docs_src/concepts/schema_migrations/block011.py [ln:1,6-16] !}
```

The [changelog](./changelogs.md) does not describe changes of schema descriptions.
