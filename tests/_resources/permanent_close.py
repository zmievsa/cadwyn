"""Exercise process exit with an upload whose disk close never returns."""

import asyncio
import json
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from io import BufferedRandom, FileIO
from pathlib import Path

import anyio
import httpx
from fastapi import Depends, UploadFile
from starlette.types import Message, Scope
from typing_extensions import override

from cadwyn import Cadwyn, Version, VersionBundle


async def main(shutdown_marker: Path, release: threading.Event, error_logged: threading.Event) -> None:
    started = threading.Event()
    uploads: list[UploadFile] = []
    logs: list[str] = []

    class Capture(logging.Handler):
        @override
        def emit(self, record: logging.LogRecord) -> None:
            logs.append(record.getMessage())
            if record.name == "cadwyn.structure.versions":
                print(record.getMessage(), flush=True)  # noqa: T201 # Assert late errors after the event loop exits.
                error_logged.set()

    logging.getLogger("cadwyn.routing").addHandler(Capture())
    worker_logger = logging.getLogger("cadwyn.structure.versions")
    worker_logger.addHandler(Capture())
    worker_logger.setLevel(logging.DEBUG)

    class BlockingFile(BufferedRandom):
        @override
        def close(self) -> None:
            if not self.closed:  # pragma: no branch # Fixture finalization guard.
                started.set()
                release.wait()
                super().close()
                raise OSError("late close failure")

    @asynccontextmanager
    async def lifespan(app: Cadwyn):
        try:
            yield
        finally:
            await anyio.Path(shutdown_marker).write_text("ASGI shutdown finished")

    app = Cadwyn(versions=VersionBundle(Version("2000-01-01")), lifespan=lifespan)

    async def capture(upload: UploadFile) -> None:
        await upload.close()
        upload.file = BlockingFile(FileIO(shutdown_marker.with_suffix(".bin"), "w+"))
        uploads.append(upload)

    @app.post("/test", dependencies=[Depends(capture)])
    async def endpoint(upload: UploadFile):
        return {"size": upload.size}

    request = httpx.Request("POST", "http://test/test", files={"upload": b"x" * (2 * 1024 * 1024)})
    body = request.read()

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":  # pragma: no branch # The first send always fails.
            raise OSError("client disconnected")

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": "/test",
        "raw_path": b"/test",
        "root_path": "",
        "query_string": b"",
        "headers": [(key.lower(), value) for key, value in request.headers.raw],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    before = time.monotonic()
    try:
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
    except OSError as exc:
        error = str(exc)
    else:
        raise AssertionError("The send failure must still propagate")
    print(  # noqa: T201 # The parent process asserts the observable result.
        json.dumps(
            {
                "close_started": started.is_set(),
                "closed": [upload.file.closed for upload in uploads],
                "elapsed_seconds": time.monotonic() - before,
                "error": error,
                "logs": logs,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    release = threading.Event()
    error_logged = threading.Event()
    asyncio.run(main(Path(sys.argv[1]), release, error_logged))
    print("EVENT LOOP EXITED", flush=True)  # noqa: T201 # The parent also checks actual process exit.
    if sys.argv[2] == "late_failure":
        release.set()
        assert error_logged.wait(2), "The abandoned worker must log its error after the event loop exits"
