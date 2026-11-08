from typing import Any, Union

from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from pydantic import BaseModel


class TaskMetadata(BaseModel):
    trace_id: str


def customize_openapi(
    schema: dict[str, Any], version: Union[str, None]
) -> dict[str, Any]:
    if version == "2024-01-01":
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        schemas["TaskMetadata"] = TaskMetadata.model_json_schema()
    return schema


app = Cadwyn(
    versions=VersionBundle(Version("2024-01-01"), Version("2023-01-01")),
    openapi_customizer=customize_openapi,
)
router = VersionedAPIRouter()


@router.get("/tasks")
def get_tasks() -> list[str]:
    return ["example-task"]


app.generate_and_include_versioned_routers(router)
