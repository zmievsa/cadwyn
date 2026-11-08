from fastapi.testclient import TestClient

from docs_src.concepts.api_version_parameter.block009 import app


def test__websocket_example__date_header_between_versions__should_return_closest_earlier_version():
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/ws", headers={"x-api-version": "2024-01-15"}
        ) as websocket,
    ):
        assert websocket.receive_json() == {"version": "2024-01-01"}
