from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi import WebSocket

versions = VersionBundle(Version("2024-02-01"), Version("2024-01-01"))
app = Cadwyn(versions=versions)
router = VersionedAPIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"version": versions.api_version_var.get()})
    await websocket.close()


app.generate_and_include_versioned_routers(router)
