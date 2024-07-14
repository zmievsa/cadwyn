# Rename a field in schema

Let's say that we had a "summary" field before but now we want to rename it to "bio".

1. Rename `summary` field to `bio` in `users.BaseUser`
2. Add the following migration to `versions.v2001_01_01`:

    ```python
    from cadwyn import (
        VersionChange,
        schema,
        convert_response_to_previous_version_for,
        convert_request_to_next_version_for,
        ResponseInfo,
        RequestInfo,
    )
    from users import BaseUser, UserCreateRequest, UserResource


    class RenameSummaryIntoBioInUser(VersionChange):
        description = (
            "Rename 'summary' field into 'bio' to keep up with industry standards"
        )
        instructions_to_migrate_to_previous_version = (
            schema(BaseUser).field("bio").had(name="summary"),
        )

        @convert_request_to_next_version_for(UserCreateRequest)
        def rename_bio_to_summary(request: RequestInfo):
            request.body["summary"] = request.body.pop("bio")

        @convert_response_to_previous_version_for(UserResource)
        def rename_bio_to_summary(response: ResponseInfo):
            response.body["bio"] = response.body.pop("summary")
    ```
