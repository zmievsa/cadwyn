from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from docs_src.concepts.endpoint_migrations.block007 import app


@pytest.mark.parametrize(
    ("version", "path", "headers"),
    [
        ("2024-02-01", "/ws/general", {"token": "current"}),
        ("2024-01-01", "/chat/general", {"legacy-token": "legacy"}),
    ],
)
def test__websocket_example__changed_path_and_dependency__should_accept_each_versions_header(
    version, path, headers
):
    with (
        TestClient(app) as client,
        client.websocket_connect(
            path, headers={"x-api-version": version, **headers}
        ) as websocket,
    ):
        assert websocket.receive_json() == {"room": "general"}


@pytest.mark.parametrize(
    ("version", "path", "expected"),
    [
        ("2024-02-01", "/notifications", "notification"),
        ("2024-01-01", "/legacy", "legacy"),
    ],
)
def test__websocket_example__route_exists__should_exchange_message(
    version, path, expected
):
    with (
        TestClient(app) as client,
        client.websocket_connect(
            path, headers={"x-api-version": version}
        ) as websocket,
    ):
        assert websocket.receive_text() == expected


@pytest.mark.parametrize(
    ("version", "path"),
    [("2024-02-01", "/legacy"), ("2024-01-01", "/notifications")],
)
def test__websocket_example__route_does_not_exist__should_reject_connection(
    version, path
):
    with (
        TestClient(app) as client,
        ExitStack() as connections,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        connections.enter_context(
            client.websocket_connect(path, headers={"x-api-version": version})
        )
    assert exc.value.code == 1000


def test__websocket_example__openapi__should_exclude_websocket_routes():
    with TestClient(app) as client:
        for version in ("2024-02-01", "2024-01-01"):
            response = client.get(f"/openapi.json?version={version}")
            assert response.status_code == 200
            assert response.json()["paths"] == {}
