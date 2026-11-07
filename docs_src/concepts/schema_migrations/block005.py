from cadwyn import (
    RequestInfo,
    ResponseInfo,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    schema,
)
from pydantic import BaseModel, Field


class MySchema(BaseModel):
    foo: str = Field(alias="foo_key")


class RenameFooKey(VersionChange):
    description = (
        "Renamed the key of 'MySchema.foo' from 'old_foo_key' to 'foo_key'"
    )
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(alias="old_foo_key"),
    )

    @convert_request_to_next_version_for(MySchema)
    def rename_old_foo_key(request: RequestInfo):
        request.body["foo_key"] = request.body.pop("old_foo_key")

    @convert_response_to_previous_version_for(MySchema)
    def rename_foo_key(response: ResponseInfo):
        response.body["old_foo_key"] = response.body.pop("foo_key")
