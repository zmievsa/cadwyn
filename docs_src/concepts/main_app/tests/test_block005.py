from typing import Any

import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel, create_model

from docs_src.concepts.main_app.block005 import (
    TraceSpan,
    app,
    customize_openapi,
)


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return own + [ref for value in node.values() for ref in _refs(value)]
    if isinstance(node, list):
        return [ref for item in node for ref in _refs(item)]
    return []


def test__openapi_customizer__nested_extra_component__should_only_appear_in_selected_version_with_resolvable_refs():
    with TestClient(app) as client:
        latest = client.get("/openapi.json?version=2024-01-01")
        older = client.get("/openapi.json?version=2023-01-01")
        tasks = client.get("/tasks", headers={"x-api-version": "2024-01-01"})

    assert latest.status_code == 200
    assert older.status_code == 200
    latest_schemas = latest.json()["components"]["schemas"]
    assert latest_schemas["TaskMetadata"] == {
        "title": "TaskMetadata",
        "type": "object",
        "properties": {
            "trace_id": {"title": "Trace Id", "type": "string"},
            "spans": {
                "title": "Spans",
                "type": "array",
                "items": {"$ref": "#/components/schemas/TraceSpan"},
            },
        },
        "required": ["trace_id", "spans"],
    }
    assert latest_schemas["TraceSpan"] == {
        "title": "TraceSpan",
        "type": "object",
        "properties": {"span_id": {"title": "Span Id", "type": "string"}},
        "required": ["span_id"],
    }
    refs = _refs(latest.json())
    assert "#/components/schemas/TraceSpan" in refs
    assert all(
        ref.removeprefix("#/components/schemas/") in latest_schemas
        for ref in refs
    )
    assert "TaskMetadata" not in older.json()["components"]["schemas"]
    assert "TraceSpan" not in older.json()["components"]["schemas"]
    assert tasks.status_code == 200
    assert tasks.json() == ["example-task"]


def _app_with_span_endpoint(span_model: type[BaseModel]) -> Cadwyn:
    span_app = Cadwyn(
        versions=VersionBundle(Version("2024-01-01")),
        openapi_customizer=customize_openapi,
    )
    router = VersionedAPIRouter()

    @router.get("/spans", response_model=span_model)
    def get_span() -> dict[str, str]:
        raise NotImplementedError

    span_app.generate_and_include_versioned_routers(router)
    return span_app


def test__openapi_customizer__component_with_same_name_and_schema__should_keep_it():
    with TestClient(_app_with_span_endpoint(TraceSpan)) as client:
        response = client.get("/openapi.json?version=2024-01-01")

    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    assert schemas["TraceSpan"]["properties"] == {
        "span_id": {"title": "Span Id", "type": "string"}
    }
    assert schemas["TaskMetadata"]["properties"]["spans"]["items"] == {
        "$ref": "#/components/schemas/TraceSpan"
    }


def test__openapi_customizer__component_with_same_name_and_other_schema__should_raise_instead_of_overwriting():
    other_trace_span = create_model("TraceSpan", duration=(int, ...))

    with (
        TestClient(_app_with_span_endpoint(other_trace_span)) as client,
        pytest.raises(ValueError, match="different 'TraceSpan' component"),
    ):
        client.get("/openapi.json?version=2024-01-01")
