from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.versions import HeadVersion, Version, VersionBundle


class Response(BaseModel):
    foo: float


router = VersionedAPIRouter()


@router.get("/test", response_model=Response)
def _test() -> Response | JSONResponse:
    return Response(foo=11.1)


def test__response_model_respected():
    app = Cadwyn(
        versions=VersionBundle(HeadVersion(), Version("2023-04-14")),
    )

    app.generate_and_include_versioned_routers(router)

    with TestClient(app) as client:
        resp = client.get("/docs")
        assert resp.status_code == 200, resp.json()

        resp = client.get("/docs?version=2023-04-14")
        assert resp.status_code == 200, resp.json()

        resp = client.get("/test")
        assert resp.status_code == 404, resp.json()

        resp = client.get("/test", headers={"X-API-VERSION": "2023-04-14"})
        assert resp.status_code == 200, resp.json()
        assert resp.json() == {"foo": 11.1}, resp.json()
