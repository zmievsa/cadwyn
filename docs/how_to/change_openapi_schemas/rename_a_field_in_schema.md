# Rename a field in schema

Let's say that we had a "summary" field before but now we want to rename it to "bio".

1. Rename `summary` field to `bio` in `users.BaseUser`
2. Add the following migration to `versions.v2001_01_01`:

```python
{! ./docs_src/how_to/change_openapi_schemas/rename_a_field_in_schema/block001.py !}
```
