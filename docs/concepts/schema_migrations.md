# Schema migrations

All of the following instructions affect only OpenAPI schemas and their initial validation. All of your incoming requests will still be converted into your HEAD schemas.

Please note that you only need to have a migration if it is a breaking change for your users. The scenarios below only describe "what you can do" but not "what you should do". For the "should" part, please refer to the [how-to docs](../how_to/change_openapi_schemas/add_field.md).

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

The following code allows to set an attribute of a field, such as a description:

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(description="Foo"),
    )
```

The following code allows to un-set an attribute of a field, as if it never existed:

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").didnt_have("description"),
    )
```

**DEFAULTS WARNING:**

If you add `default` or `default_factory` into the old version of a schema -- it will not manifest in code automatically. Instead, you should add both the `default` or `default_factory`, and then also add the default value using a request migration.

The reason for such behaviour is the way how Cadwyn works with pydantic and unfortunately this cannot be changed:

Cadwyn:

1. Receives a request for API version `V`
2. Validates the request using the schemas from `V`
3. Marshalls the unmarshalled request body into a raw data structure using `BaseModel.dict` (`BaseModel.model_dump` in Pydantic v2) using **exclude_unset=True**
4. Passes the request through all request migrations from `V` to `latest`
5. Validates the request using `latest` schemas

The part that causes the aforementioned problem is cadwyn's usage of `exclude_unset=True`. Unfortunately, when we use it, all default fields do not get set, so `latest` does not receive them. And if `latest` does not have the same defaults (for example, if the field has no default and is required in `latest`), then an error will occur. If we used `exclude_unset=False`, then `exclude_unset` would lose all its purpose for the users of our library so we cannot give it up. Instead, you should set all extras on step 4 in your request migrations.

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

The following code allows to replace all schema name occurrences with the new one to make sure that the name is different in openapi.json:


```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).had(name="OtherSchema"),
    )
```
