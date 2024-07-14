# Enum migrations

All of the following instructions affect only openapi schemas and their initial validation. All of your incoming requests will still be converted into your HEAD schemas.

## Adding enum members

Note that adding enum members **can** be a breaking change unlike adding optional fields to a schema. For example, if I return a list of entities, each of which has some type, and I add a new type -- then my client's code is likely to break.

So I suggest adding enum members in new versions as well.

```python
from cadwyn import VersionChange, enum
from enum import auto


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        enum(my_enum).had(foo="baz", bar=auto()),
    )
```

## Removing enum members

```python
from cadwyn import VersionChange, enum


class MyChange(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = (
        enum(my_enum).didnt_have("foo", "bar"),
    )
```
