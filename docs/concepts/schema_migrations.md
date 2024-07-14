# Schema migrations

All of the following instructions affect only openapi schemas and their initial validation. All of your incoming requests will still be converted into your HEAD schemas.

## Add a field to the older version

```python
from pydantic import Field
from cadwyn import VersionChange, schema


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

If you would like to set a description or any other attribute of a field, you would do:

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(description="Foo"),
    )
```

and if you would like to unset any attribute of a field as if it was never passed, you would do:

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

This happens because of how Cadwyn works with pydantic and sadly cannot be changed:

Cadwyn:

1. Receives the request of some version `V`
2. Validates the request using the schemas from `V`
3. Marshalls the unmarshalled request body into a raw data structure using `BaseModel.dict` (`BaseModel.model_dump` in Pydantic v2) using **exclude_unset=True**
4. Passes the request through all request migrations from `V` to `latest`
5. Validates the request using `latest` schemas

The part that causes the aforementioned problem is our usage of `exclude_unset=True`. Sadly, when we use it, all default fields do not get set so `latest` does not receive them. And if `latest` does not have the same defaults (for example, if the field has no default and is required in `latest`), then an error will occur. If we used `exclude_unset=False`, then `exclude_unset` would lose all its purpose for the users of our library so we cannot abandon it. Instead, you should set all extra on step 4 in your request migrations.

## Add a validator to the older version

```python
from pydantic import Field, field_validator
from cadwyn import VersionChange, schema


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
from pydantic import Field, validator
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).validator(MySchema.validate_foo).didnt_exist,
    )
```

## Rename a schema in the older version

If you wish to rename your schema to make sure that its name is different in openapi.json:

```python
from cadwyn import VersionChange, schema


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).had(name="OtherSchema"),
    )
```

which will replace all references to this schema with the new name.
