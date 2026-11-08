from fastapi.testclient import TestClient

from docs_src.concepts.schema_migrations.block013 import app

client = TestClient(app)


def test__schema_config__older_request_and_response__should_migrate_with_forbidden_extras():
    response = client.post(
        "/users",
        json={"name": "Alice"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"name": "Alice"}


def test__schema_config__older_request_with_unknown_key__should_fail_before_migration():
    response = client.post(
        "/users",
        json={"name": "Alice", "nickname": "Al"},
        headers={"x-api-version": "2000-01-01"},
    )
    assert response.status_code == 422, response.json()
    assert response.json()["detail"] == [
        {
            "type": "extra_forbidden",
            "loc": ["body", "nickname"],
            "msg": "Extra inputs are not permitted",
            "input": "Al",
        }
    ]


def test__schema_config__newer_request_with_extra__should_preserve_extra():
    response = client.post(
        "/users",
        json={"name": "Alice", "nickname": "Al", "client_note": "keep"},
        headers={"x-api-version": "2001-01-01"},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "name": "Alice",
        "nickname": "Al",
        "client_note": "keep",
        "server_note": "created",
    }


def test__schema_config__openapi__should_forbid_extras_only_in_older_version():
    old_schemas = client.get("/openapi.json?version=2000-01-01").json()[
        "components"
    ]["schemas"]
    new_schemas = client.get("/openapi.json?version=2001-01-01").json()[
        "components"
    ]["schemas"]
    assert old_schemas["User"]["additionalProperties"] is False
    assert new_schemas["User"]["additionalProperties"] is True
    assert "nickname" not in old_schemas["User"]["properties"]
    assert "nickname" in new_schemas["User"]["properties"]
