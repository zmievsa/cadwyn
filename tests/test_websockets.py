from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack
from contextvars import ContextVar
from typing import Annotated, Literal

import anyio
import pytest
from fastapi import Depends, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from cadwyn.exceptions import CadwynError


@pytest.fixture(params=["custom_header", "path"])
def location(request: pytest.FixtureRequest) -> Literal["custom_header", "path"]:
    return request.param


def _app(
    location: Literal["custom_header", "path"],
    *,
    default: str | Callable[[Request], Awaitable[str]] | None = None,
    version_format: Literal["date", "string"] = "date",
) -> Cadwyn:
    values = ("2024-02-01", "2024-01-01") if version_format == "date" else ("v2", "v1")
    versions = VersionBundle(*(Version(value) for value in values), api_version_var=ContextVar("websocket_version"))
    app = Cadwyn(
        versions=versions,
        api_version_location=location,
        api_version_parameter_name="api_version",
        api_version_format=version_format,
        api_version_default_value=default,
    )
    router = VersionedAPIRouter(prefix="/{api_version}" if location == "path" else "")

    async def dependency() -> AsyncIterator[str | None]:
        version = versions.api_version_var.get(None)
        try:
            yield version
        finally:
            assert versions.api_version_var.get(None) == version

    @router.websocket("/ws")
    async def socket(websocket: WebSocket, dependency_version: Annotated[str | None, Depends(dependency)]):
        await websocket.accept()
        await websocket.send_json(
            {
                "dependency": dependency_version,
                "context": versions.api_version_var.get(None),
                "scope": websocket.scope["cadwyn.api_version"],
                "path_params": websocket.path_params,
            }
        )
        message = await websocket.receive_text()
        if message == "error":
            raise RuntimeError("endpoint failed")
        await websocket.send_json({"message": message, "context": versions.api_version_var.get(None)})
        await websocket.close()

    @router.get("/ping")
    async def ping():
        return "pong"

    app.generate_and_include_versioned_routers(router)
    return app


def _connect(client: TestClient, location: str, version: str | None):
    path = f"/{version}/ws" if location == "path" and version is not None else "/ws"
    headers = {"api_version": version} if location == "custom_header" and version is not None else {}
    return client.websocket_connect(path, headers=headers)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("2024-02-01", "2024-02-01"),
        ("2024-01-01", "2024-01-01"),
        ("2024-01-15", "2024-01-01"),
        ("2025-01-01", "2024-02-01"),
    ],
)
def test__versioned_websocket__header_or_path_version__should_select_version_before_dependencies(
    location: Literal["custom_header", "path"], requested: str, expected: str
):
    with TestClient(_app(location)) as client, _connect(client, location, requested) as socket:
        assert socket.receive_json() == {
            "dependency": expected,
            "context": expected,
            "scope": expected,
            "path_params": {"api_version": requested} if location == "path" else {},
        }
        socket.send_text("hello")
        assert socket.receive_json() == {"message": "hello", "context": expected}


def test__versioned_websocket__constant_default__should_select_closest_earlier_version():
    with TestClient(_app("custom_header", default="2024-01-15")) as client, client.websocket_connect("/ws") as socket:
        assert socket.receive_json()["dependency"] == "2024-01-01"
        socket.send_text("hello")
        assert socket.receive_json() == {"message": "hello", "context": "2024-01-01"}


def test__versioned_websocket__plain_starlette_socket__should_select_api_version(
    location: Literal["custom_header", "path"],
):
    app = _app(location)
    router = VersionedAPIRouter()

    async def plain_socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json(app.versions.api_version_var.get(None))
        await websocket.close()

    router.add_websocket_route("/{api_version}/plain/ws" if location == "path" else "/plain/ws", plain_socket)
    app.generate_and_include_versioned_routers(router)
    path = "/2024-01-15/plain/ws" if location == "path" else "/plain/ws"
    with TestClient(app) as client, client.websocket_connect(path, headers={"api_version": "2024-01-15"}) as socket:
        assert socket.receive_json() == "2024-01-01"


@pytest.mark.parametrize("version", [None, "", "bad", "2024-99-01", "2000-01-01"])
def test__versioned_websocket__missing_or_invalid_version__should_reject_before_accepting(
    location: Literal["custom_header", "path"], version: str | None
):
    with (
        TestClient(_app(location)) as client,
        ExitStack() as connections,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        connections.enter_context(_connect(client, location, version))
    assert exc.value.code == 1008
    assert "API version" in exc.value.reason


@pytest.mark.parametrize("version", ["v1", "v2"])
def test__versioned_websocket__string_version__should_select_exact_version(
    location: Literal["custom_header", "path"], version: str
):
    with TestClient(_app(location, version_format="string")) as client, _connect(client, location, version) as socket:
        assert socket.receive_json()["dependency"] == version
        socket.send_text("hello")
        assert socket.receive_json() == {"message": "hello", "context": version}


def test__versioned_websocket__unknown_string_version__should_reject_before_accepting(
    location: Literal["custom_header", "path"],
):
    with (
        TestClient(_app(location, version_format="string")) as client,
        ExitStack() as connections,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        connections.enter_context(_connect(client, location, "v3"))
    assert exc.value.code == 1008
    assert "API version" in exc.value.reason


async def _request_default(request: Request) -> str:
    assert request.scope["type"] == "http"
    return "2024-01-01"


def test__http__callable_default_alongside_websockets__should_keep_request_default_and_response():
    with TestClient(_app("custom_header", default=_request_default)) as client:
        response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == "pong"
    assert response.headers["api_version"] == "2024-01-01"


def test__versioned_websocket__request_default_needed__should_raise_clear_unsupported_error():
    with (
        TestClient(_app("custom_header", default=_request_default)) as client,
        ExitStack() as connections,
        pytest.raises(CadwynError, match=r"callable.*api_version_default_value.*WebSocket"),
    ):
        connections.enter_context(client.websocket_connect("/ws"))


def test__versioned_websocket__explicit_version_with_callable_default__should_connect():
    with (
        TestClient(_app("custom_header", default=_request_default)) as client,
        _connect(client, "custom_header", "2024-01-01") as socket,
    ):
        assert socket.receive_json()["dependency"] == "2024-01-01"
        socket.send_text("hello")
        assert socket.receive_json()["context"] == "2024-01-01"


@pytest.mark.parametrize(
    ("location", "default"),
    [("custom_header", None), ("custom_header", "2024-02-01"), ("custom_header", _request_default), ("path", None)],
)
@pytest.mark.parametrize("version", [None, "bad", "2024-02-01"])
def test__unversioned_websocket__version_header_or_default__should_keep_context_unset(
    default, version, location: Literal["custom_header", "path"]
):
    app = _app(location, default=default)
    router = VersionedAPIRouter(prefix="/{api_version}" if location == "path" else "")

    @router.websocket("/ws")
    async def unversioned(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json(app.versions.api_version_var.get(None))
        await websocket.close()

    app.include_router(router)
    requested = version if location == "custom_header" else version or "unversioned"
    with TestClient(app) as client, _connect(client, location, requested) as socket:
        assert socket.receive_json() is None


def test__unversioned_websocket__empty_preselected_version__should_connect_and_preserve_context():
    app = _app("custom_header", default=_request_default)
    after = []

    @app.websocket("/plain")
    async def plain(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json(
            {"version": app.versions.api_version_var.get(None), "scope": websocket.scope["cadwyn.api_version"]}
        )
        await websocket.close()

    async def preselect(scope: Scope, receive: Receive, send: Send):
        token = app.versions.api_version_var.set("")
        try:
            await app(scope, receive, send)
        finally:
            if scope["type"] == "websocket":
                after.append(app.versions.api_version_var.get(None))
            app.versions.api_version_var.reset(token)

    with TestClient(preselect) as client, client.websocket_connect("/plain") as websocket:
        assert websocket.receive_json() == {"version": "", "scope": ""}
    assert after == [""]


@pytest.mark.parametrize("preselected", [None, "2024-01-15"])
@pytest.mark.parametrize("message", ["hello", "error"])
def test__versioned_websocket__connection_ends__should_restore_context_even_after_waterfall_or_exception(
    preselected: str | None, message: str
):
    app = _app("custom_header", default=_request_default)
    after = []

    async def observe(scope: Scope, receive: Receive, send: Send):
        token = app.versions.api_version_var.set(preselected)
        try:
            await app(scope, receive, send)
        finally:
            if scope["type"] == "websocket":
                after.append(app.versions.api_version_var.get(None))
            app.versions.api_version_var.reset(token)

    def connect(client: TestClient):
        with _connect(client, "custom_header", "2024-02-01") as socket:
            expected = "2024-01-01" if preselected else "2024-02-01"
            assert socket.receive_json()["dependency"] == expected
            socket.send_text(message)
            assert socket.receive_json() == {"message": message, "context": expected}

    with TestClient(observe) as client:
        if message == "error":
            with pytest.raises(RuntimeError, match="endpoint failed"):
                connect(client)
        else:
            connect(client)
    assert after == [preselected]


def test__versioned_websocket__simultaneous_connections__should_keep_versions_separate():
    with TestClient(_app("custom_header")) as client, _connect(client, "custom_header", "2024-02-01") as newer:
        assert newer.receive_json()["context"] == "2024-02-01"
        with _connect(client, "custom_header", "2024-01-01") as older:
            assert older.receive_json()["context"] == "2024-01-01"
            newer.send_text("newer")
            assert newer.receive_json() == {"message": "newer", "context": "2024-02-01"}
            older.send_text("older")
            assert older.receive_json() == {"message": "older", "context": "2024-01-01"}


def test__versioned_websocket__connection_cancelled__should_restore_context_after_dependency_cleanup():
    app = _app("custom_header")
    after = []

    async def cancel_connection(scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "websocket":
            await app(scope, receive, send)
            return
        with anyio.CancelScope() as cancel_scope:

            async def cancel_after_message(message: Message):
                await send(message)
                if message["type"] == "websocket.send":
                    cancel_scope.cancel()

            try:
                await app(scope, receive, cancel_after_message)
            finally:
                after.append(app.versions.api_version_var.get(None))

    with TestClient(cancel_connection) as client, _connect(client, "custom_header", "2024-01-15") as socket:
        assert socket.receive_json()["dependency"] == "2024-01-01"
    assert after == [None]


def test__versioned_websocket__openapi__should_keep_websockets_out_of_every_schema(
    location: Literal["custom_header", "path"],
):
    with TestClient(_app(location)) as client:
        for version in ("2024-02-01", "2024-01-01"):
            schema = client.get(f"/openapi.json?version={version}")
            assert schema.status_code == 200
            assert list(schema.json()["paths"]) == ["/{api_version}/ping" if location == "path" else "/ping"]
