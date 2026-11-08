from fastapi.testclient import TestClient

from docs_src.concepts.main_app.block005 import app


def test__openapi_customizer__extra_component__should_only_appear_in_selected_version():
    with TestClient(app) as client:
        latest = client.get("/openapi.json?version=2024-01-01")
        older = client.get("/openapi.json?version=2023-01-01")
        tasks = client.get("/tasks", headers={"x-api-version": "2024-01-01"})

    assert latest.status_code == 200
    assert older.status_code == 200
    assert latest.json()["components"]["schemas"]["TaskMetadata"] == {
        "title": "TaskMetadata",
        "type": "object",
        "properties": {"trace_id": {"title": "Trace Id", "type": "string"}},
        "required": ["trace_id"],
    }
    assert "TaskMetadata" not in older.json()["components"]["schemas"]
    assert tasks.status_code == 200
    assert tasks.json() == ["example-task"]
