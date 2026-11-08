from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    endpoint,
)
from fastapi import Depends, Header, WebSocket


async def current_token(token: str = Header()):
    return token


async def legacy_token(legacy_token: str = Header()):
    return legacy_token


router = VersionedAPIRouter()


@router.websocket(
    "/ws/{room}", name="room", dependencies=[Depends(current_token)]
)
async def room_socket(websocket: WebSocket, room: str):
    await websocket.accept()
    await websocket.send_json({"room": room})
    await websocket.close()


@router.websocket("/notifications")
async def notifications(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("notification")
    await websocket.close()


@router.only_exists_in_older_versions
@router.websocket("/legacy")
async def legacy_socket(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("legacy")
    await websocket.close()


class UpdateSockets(VersionChange):
    description = "Move chat connections and their token header; replace the legacy socket."
    instructions_to_migrate_to_previous_version = (
        endpoint("/ws/{room}", ["WEBSOCKET"]).had(
            path="/chat/{room}",
            name="chat",
            dependencies=[Depends(legacy_token)],
        ),
        endpoint("/notifications", ["WEBSOCKET"]).didnt_exist,
        endpoint("/legacy", ["WEBSOCKET"]).existed,
    )


app = Cadwyn(
    versions=VersionBundle(
        Version("2024-02-01", UpdateSockets), Version("2024-01-01")
    )
)
app.generate_and_include_versioned_routers(router)
