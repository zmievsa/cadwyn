# Schema migrations

The following instructions affect only OpenAPI schemas and their initial validation. All your incoming requests will still be converted to your HEAD schemas.

Please note that you only need a migration if it is a breaking change for your users. The scenarios below only describe "what you can do" but not "what you should do". For the "should" part, please refer to the [how-to docs](../how_to/change_openapi_schemas/add_field.md).

## Add a field to the older version

```python
from cadwyn import VersionChange, schema
from pydantic import Field


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema)
        .field("foo")
        .existed_as(type=list[str], info=Field(description="Foo")),
    )
```

## Remove a field from the older version

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").didnt_exist,
    )
```

## Change a field in the older version

The following code sets an attribute of a field, such as a description:

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(description="Foo"),
    )
```

The following code un-sets an attribute of a field, as if it never existed:

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").didnt_have("description"),
    )
```

**DEFAULTS WARNING:**

If you add `default` or `default_factory` to the old version of a schema, it will not manifest in the code automatically. Instead, you should add both the `default` and `default_factory`, and then also add the default value using a request migration.

The reason for such behaviour is the way Cadwyn works with Pydantic and unfortunately this cannot be changed:

Cadwyn:

1. Receives a request for API version `V`
2. Validates the request using the schemas from `V`
3. Marshalls the unmarshalled request body into a raw data structure using `BaseModel.dict` (`BaseModel.model_dump` in Pydantic v2) with **exclude_unset=True**
4. Passes the request through all request migrations from `V` to `latest`
5. Validates the request using `latest` schemas

The part that causes the aforementioned problem is Cadwyn's use of `exclude_unset=True`. Unfortunately, when Cadwyn uses it, default fields are not set, so `latest` does not receive them. And if latest does not have the same defaults (for example, if the field has no default and is required in latest), then an error will occur. If Cadwyn used `exclude_unset=False`, then `exclude_unset` would lose its purpose for Cadwyn users, so giving it up is impractical. Instead, you should set all extras in step 4 in your request migrations.

## Add a validator to the older version

```python
from cadwyn import VersionChange, schema
from pydantic import Field, field_validator


@field_validator("foo")
def validate_foo(cls, value):
    if not ":" in value:
        raise TypeError
    return value


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).validator(validate_foo).existed,
    )
```

## Remove a validator from the older version

```python
from cadwyn import VersionChange, schema
from pydantic import Field, validator


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).validator(MySchema.validate_foo).didnt_exist,
    )
```

## Rename a schema in the older version

The following code replaces all schema name occurrences with a new one to ensure that the name is different in openapi.json:


```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).had(name="OtherSchema"),
    )
```
