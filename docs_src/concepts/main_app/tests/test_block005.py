from typing import Any

from fastapi.testclient import TestClient

from docs_src.concepts.main_app.block005 import app


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
