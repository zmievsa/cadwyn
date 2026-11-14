from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from cadwyn import Cadwyn, Version, VersionBundle


def test__plain_json_response__with_exact_application_json__should_not_double_encode():
    router = APIRouter()

    @router.get("/plain_json")
    def plain_json():
        return Response(content=b'{"a": 1}', media_type="application/json")

    @router.get("/plain_json_charset")
    def plain_json_charset():
        return Response(
            content=b'{"a": 1}',
            headers={"content-type": "application/json; charset=utf-8"},
        )

    @router.get("/jsonresponse")
    def jsonresponse():
        return JSONResponse({"a": 1})

    @router.get("/plain_text")
    def plain_text():
        return PlainTextResponse("hi")

    app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))
    app.generate_and_include_versioned_routers(router)
    client = TestClient(app)

    r = client.get("/plain_json", headers={"x-api-version": "2000-01-01"})
    assert r.status_code == 200
    assert r.content == b'{"a":1}' or r.content == b'{"a": 1}'  # compact or original spacing acceptable
    assert r.json() == {"a": 1}

    r = client.get("/plain_json_charset", headers={"x-api-version": "2000-01-01"})
    assert r.status_code == 200
    assert r.json() == {"a": 1}

    r = client.get("/jsonresponse", headers={"x-api-version": "2000-01-01"})
    assert r.status_code == 200
    assert r.json() == {"a": 1}

    r = client.get("/plain_text", headers={"x-api-version": "2000-01-01"})
    assert r.status_code == 200
    assert r.text == "hi"
