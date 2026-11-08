from collections.abc import AsyncIterator
from contextlib import ExitStack
from typing import Annotated, Literal

import pytest
from fastapi import Depends, FastAPI, Header, Query, WebSocket
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter, endpoint
from cadwyn.exceptions import LintingError, RouterGenerationError, RouterPathParamsModifiedError
from cadwyn.route_generation import generate_versioned_routers
from cadwyn.structure.data import convert_request_to_next_version_for, convert_response_to_previous_version_for
from tests.conftest import version_change


def _app(router: VersionedAPIRouter, *instructions, location: Literal["custom_header", "path"] = "custom_header"):
    app = Cadwyn(
        versions=VersionBundle(Version("2024-02-01", version_change(*instructions)), Version("2024-01-01")),
        api_version_location=location,
        api_version_parameter_name="api_version",
    )
    app.generate_and_include_versioned_routers(router)
    return app


def test__websocket_didnt_exist__same_path_as_http__should_delete_only_older_socket():
    router = VersionedAPIRouter()

    @router.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("latest")
        await websocket.close()

    @router.get("/ws")
    async def http():
        return "http"

    app = _app(router, endpoint("/ws", ["WEBSOCKET"]).didnt_exist)
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"api_version": "2024-02-01"}) as websocket:
            assert websocket.receive_text() == "latest"
        with ExitStack() as connections, pytest.raises(WebSocketDisconnect) as exc:
            connections.enter_context(client.websocket_connect("/ws", headers={"api_version": "2024-01-01"}))
        assert exc.value.code == 1000
        for version in ("2024-02-01", "2024-01-01"):
            assert client.get("/ws", headers={"api_version": version}).json() == "http"
            assert set(client.get(f"/openapi.json?version={version}").json()["paths"]["/ws"]) == {"get"}


@pytest.mark.parametrize("included", [False, True])
def test__websocket_existed__marked_as_old_only__should_restore_only_older_socket(included: bool):
    child = VersionedAPIRouter()

    @child.only_exists_in_older_versions
    @child.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("restored")
        await websocket.close()

    router = VersionedAPIRouter()
    router.include_router(child, prefix="/nested" if included else "")
    path = "/nested/ws" if included else "/ws"
    app = _app(router, endpoint(path, ["WEBSOCKET"]).existed)
    with TestClient(app) as client:
        with client.websocket_connect(path, headers={"api_version": "2024-01-01"}) as websocket:
            assert websocket.receive_text() == "restored"
        with ExitStack() as connections, pytest.raises(WebSocketDisconnect):
            connections.enter_context(client.websocket_connect(path, headers={"api_version": "2024-02-01"}))


@pytest.mark.parametrize("location", ["custom_header", "path"])
def test__websocket_had__path_name_and_dependencies_changed__should_execute_each_versions_dependencies(
    location: Literal["custom_header", "path"],
):
    observed = []

    async def latest(websocket: WebSocket, token: Annotated[str, Header()]) -> AsyncIterator[None]:
        observed.append(("latest", token, websocket.path_params["item_id"]))
        try:
            yield
        finally:
            observed.append("latest cleanup")

    async def old(websocket: WebSocket, legacy_token: Annotated[str, Header()]) -> AsyncIterator[None]:
        observed.append(("old", legacy_token, websocket.path_params["item_id"]))
        try:
            yield
        finally:
            observed.append("old cleanup")

    router = VersionedAPIRouter(prefix="/{api_version}" if location == "path" else "")

    @router.websocket("/ws/{item_id:int}", name="latest", dependencies=[Depends(latest)])
    async def socket(websocket: WebSocket, item_id: int):
        await websocket.accept()
        await websocket.send_json({"id": item_id, "name": websocket.scope["route"].name})
        await websocket.close()

    prefix = "/{api_version}" if location == "path" else ""
    app = _app(
        router,
        endpoint(prefix + "/ws/{item_id:int}", ["WEBSOCKET"]).had(
            path=prefix + "/old/{item_id:int}", name="old", dependencies=[Depends(old)]
        ),
        location=location,
    )
    with TestClient(app) as client:
        for version, path, name, headers in [
            ("2024-01-01", "/old/42", "old", {"legacy-token": "legacy"}),
            ("2024-02-01", "/ws/83", "latest", {"token": "current"}),
            ("2024-01-01", "/old/42", "old", {"legacy-token": "legacy"}),
        ]:
            url = f"/{version}{path}" if location == "path" else path
            with client.websocket_connect(url, headers={"api_version": version, **headers}) as websocket:
                assert websocket.receive_json() == {"id": int(path.split("/")[-1]), "name": name}
    assert observed == [
        ("old", "legacy", 42),
        "old cleanup",
        ("latest", "current", 83),
        "latest cleanup",
        ("old", "legacy", 42),
        "old cleanup",
    ]


@pytest.mark.parametrize("override", [False, True])
def test__generate_versioned_routers__websocket_had__should_build_each_versions_matcher_and_handler(override: bool):
    async def latest(websocket: WebSocket):
        websocket.state.value = 0

    async def older(websocket: WebSocket, value: Annotated[int, Query()]):
        websocket.state.value = value

    async def overridden(websocket: WebSocket, value: Annotated[int, Query()]):
        websocket.state.value = value * 10

    provider = FastAPI()
    provider.dependency_overrides = {older: overridden} if override else {}
    factor = 10 if override else 1
    router = VersionedAPIRouter(dependency_overrides_provider=provider)

    @router.websocket("/ws/{item_id}", name="latest", dependencies=[Depends(latest)])
    async def socket(websocket: WebSocket, item_id: int):
        await websocket.accept()
        await websocket.send_json(
            {"id": item_id, "value": websocket.state.value, "name": websocket.scope["route"].name}
        )
        await websocket.close()

    generated = generate_versioned_routers(
        router,
        VersionBundle(
            Version(
                "2024-02-01",
                version_change(
                    endpoint("/ws/{item_id}", ["WEBSOCKET"]).had(
                        path="/old/{item_id}", name="older", dependencies=[Depends(older)]
                    )
                ),
            ),
            Version("2024-01-01"),
        ),
    )
    for version, path, expected in [
        ("2024-01-01", "/old/42?value=8", {"id": 42, "value": 8 * factor, "name": "older"}),
        ("2024-02-01", "/ws/42?value=8", {"id": 42, "value": 0, "name": "latest"}),
        ("2024-01-01", "/old/42?value=9", {"id": 42, "value": 9 * factor, "name": "older"}),
    ]:
        app = Starlette(routes=generated.endpoints[version].routes)
        with TestClient(app) as client, client.websocket_connect(path) as websocket:
            assert websocket.receive_json() == expected


@pytest.mark.parametrize("methods", [["WEBSOCKET"], ["GET", "WEBSOCKET"]])
def test__http_had__websocket_method__should_reject_at_definition(methods):
    with pytest.raises(LintingError, match="HTTP methods are not valid: WEBSOCKET"):
        endpoint("/ws", ["GET"]).had(methods=methods)


def test__websocket_had__included_router_dependencies__should_replace_the_inherited_list():
    observed = []

    async def parent_dependency():
        observed.append("parent")

    async def current_dependency():
        observed.append("current")

    async def legacy_dependency():
        observed.append("legacy")

    child = VersionedAPIRouter()

    @child.websocket("/ws", dependencies=[Depends(current_dependency)])
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")
        await websocket.close()

    router = VersionedAPIRouter(dependencies=[Depends(parent_dependency)])
    router.include_router(child, prefix="/nested")
    app = _app(router, endpoint("/nested/ws", ["WEBSOCKET"]).had(dependencies=[Depends(legacy_dependency)]))
    with TestClient(app) as client:
        with client.websocket_connect("/nested/ws", headers={"api_version": "2024-01-01"}) as websocket:
            assert websocket.receive_text() == "connected"
        assert observed == ["legacy"]
        with client.websocket_connect("/nested/ws", headers={"api_version": "2024-02-01"}) as websocket:
            assert websocket.receive_text() == "connected"
    assert observed == ["legacy", "parent", "current"]


@pytest.mark.parametrize("methods", [["WEBSOCKET", "GET"], ["POST", "WEBSOCKET"], ["WEBSOCKET", "TRACE"]])
def test__endpoint__websocket_mixed_with_other_methods__should_reject_at_definition(methods):
    with pytest.raises(LintingError, match=r"WEBSOCKET.*combined"):
        endpoint("/ws", methods)


@pytest.mark.parametrize(
    "attributes",
    [
        {"status_code": 200},
        {"response_model": str},
        {"tags": []},
        {"methods": ["GET"]},
        {"include_in_schema": False},
        {"summary": "ignored"},
        {"openapi_extra": {}},
        {"callbacks": []},
    ],
)
def test__websocket_had__http_only_attribute__should_reject_at_definition(attributes):
    with pytest.raises(LintingError, match=r"WebSocket.*path.*name.*dependencies"):
        endpoint("/ws", ["WEBSOCKET"]).had(**attributes)


@pytest.mark.parametrize("converter", [convert_request_to_next_version_for, convert_response_to_previous_version_for])
def test__data_converter__websocket_method__should_keep_rejecting_at_definition(converter):
    with pytest.raises(LintingError, match="HTTP methods are not valid: WEBSOCKET"):
        converter("/ws", ["WEBSOCKET"])


def test__websocket_had__changed_path_parameters__should_raise_router_path_error():
    router = VersionedAPIRouter()

    @router.websocket("/ws/{item_id}")
    async def socket(websocket: WebSocket, item_id: int):
        raise NotImplementedError

    with (
        pytest.raises(RouterPathParamsModifiedError, match="path params"),
        TestClient(_app(router, endpoint("/ws/{item_id}", ["WEBSOCKET"]).had(path="/old/{other_id}"))),
    ):
        raise AssertionError("Incompatible path accepted")


def test__websocket_existed__marked_but_never_restored__should_raise_router_generation_error():
    router = VersionedAPIRouter()

    @router.only_exists_in_older_versions
    @router.websocket("/ws")
    async def socket(websocket: WebSocket):
        raise NotImplementedError

    with pytest.raises(RouterGenerationError, match="never restored"), TestClient(_app(router)):
        raise AssertionError("Unrestored socket accepted")


def test__websocket_existed__duplicate_path_with_function_names__should_replace_newer_socket():
    router = VersionedAPIRouter()

    @router.only_exists_in_older_versions
    @router.websocket("/ws")
    async def older(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("older")
        await websocket.close()

    @router.websocket("/ws")
    async def newer(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("newer")
        await websocket.close()

    app = _app(
        router,
        endpoint("/ws", ["WEBSOCKET"], func_name="older").existed,
        endpoint("/ws", ["WEBSOCKET"], func_name="newer").didnt_exist,
    )
    with TestClient(app) as client:
        for version, expected in [("2024-01-01", "older"), ("2024-02-01", "newer")]:
            with client.websocket_connect("/ws", headers={"api_version": version}) as websocket:
                assert websocket.receive_text() == expected


def test__websocket_existence__deleted_and_restored_across_versions__should_keep_each_versions_state():
    router = VersionedAPIRouter()

    @router.only_exists_in_older_versions
    @router.websocket("/ws")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("restored")
        await websocket.close()

    app = Cadwyn(
        versions=VersionBundle(
            Version("2024-04-01", version_change(endpoint("/ws", ["WEBSOCKET"]).existed)),
            Version("2024-03-01", version_change(endpoint("/ws", ["WEBSOCKET"]).didnt_exist)),
            Version("2024-02-01", version_change(endpoint("/ws", ["WEBSOCKET"]).existed)),
            Version("2024-01-01"),
        )
    )
    app.generate_and_include_versioned_routers(router)
    with TestClient(app) as client:
        for version in ("2024-01-01", "2024-03-01"):
            with client.websocket_connect("/ws", headers={"x-api-version": version}) as websocket:
                assert websocket.receive_text() == "restored"
        for version in ("2024-02-01", "2024-04-01"):
            with ExitStack() as connections, pytest.raises(WebSocketDisconnect) as exc:
                connections.enter_context(client.websocket_connect("/ws", headers={"x-api-version": version}))
            assert exc.value.code == 1000


@pytest.mark.parametrize(
    ("instructions", "error"),
    [
        (lambda: [endpoint("/ws", ["WEBSOCKET"]).existed], "already existed"),
        (lambda: [endpoint("/ws", ["WEBSOCKET"]).didnt_exist] * 2, "already deleted"),
        (lambda: [endpoint("/ws", ["WEBSOCKET"]).had(name="socket")], "was the same"),
        (lambda: [endpoint("/missing", ["WEBSOCKET"]).didnt_exist], "doesn't exist"),
    ],
)
def test__websocket_instruction__invalid_change__should_raise_clear_generation_error(instructions, error: str):
    router = VersionedAPIRouter()

    @router.websocket("/ws")
    async def socket(websocket: WebSocket):
        raise NotImplementedError

    with pytest.raises(RouterGenerationError, match=error), TestClient(_app(router, *instructions())):
        raise AssertionError("Invalid instruction accepted")


def test__websocket_instruction__plain_starlette_socket__should_reject_unsupported_route():
    router = VersionedAPIRouter()

    async def socket(websocket: WebSocket):
        raise NotImplementedError

    router.add_websocket_route("/ws", socket)
    with (
        pytest.raises(RouterGenerationError, match="doesn't exist"),
        TestClient(_app(router, endpoint("/ws", ["WEBSOCKET"]).didnt_exist)),
    ):
        raise AssertionError("Plain Starlette route migrated")


def test__only_exists_in_older_versions__function_also_registered_as_websocket__should_keep_http_selection():
    router = VersionedAPIRouter()

    @router.only_exists_in_older_versions
    @router.get("/shared")
    @router.websocket("/shared")
    async def shared(connection: HTTPConnection):
        if isinstance(connection, WebSocket):
            await connection.accept()
            await connection.send_text("websocket")
            await connection.close()
        return "http"

    app = _app(router, endpoint("/shared", ["GET"]).existed)
    with TestClient(app) as client:
        assert client.get("/shared", headers={"api_version": "2024-01-01"}).json() == "http"
        assert client.get("/shared", headers={"api_version": "2024-02-01"}).status_code == 404
        with client.websocket_connect("/shared", headers={"api_version": "2024-02-01"}) as websocket:
            assert websocket.receive_text() == "websocket"


def test__only_exists_in_older_versions__different_websocket_function__should_raise_lookup_error():
    router = VersionedAPIRouter()

    @router.websocket("/registered")
    async def registered(websocket: WebSocket):
        raise NotImplementedError

    async def unregistered(websocket: WebSocket):
        raise NotImplementedError

    with pytest.raises(LookupError, match="Route not found on endpoint"):
        router.only_exists_in_older_versions(unregistered)
