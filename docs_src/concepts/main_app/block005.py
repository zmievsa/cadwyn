from typing import Any, Union

from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from pydantic import BaseModel


class TraceSpan(BaseModel):
    span_id: str


class TaskMetadata(BaseModel):
    trace_id: str
    spans: list[TraceSpan]


def customize_openapi(
    schema: dict[str, Any], version: Union[str, None]
) -> dict[str, Any]:
    if version == "2024-01-01":
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        metadata_schema = TaskMetadata.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        # Nested models such as TraceSpan become components of their own
        new_schemas = metadata_schema.pop("$defs", {})
        new_schemas["TaskMetadata"] = metadata_schema
        for name, component in new_schemas.items():
            # Never overwrite a component that endpoints already reference
            if schemas.setdefault(name, component) != component:
                raise ValueError(
                    f"OpenAPI already has a different {name!r} component"
                )
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
