# Rename a field in schema

Assume you had a `summary` field before but now you want to rename it to `bio`.

1. Rename the `summary` field to `bio` in `users.BaseUser`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    {!> ./docs_src/how_to/change_openapi_schemas/rename_a_field_in_schema/rename_summary_to_bio/versions/v2001_01_01.py [ln:2-28] !}
    ```
