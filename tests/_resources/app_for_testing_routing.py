from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, Response

from cadwyn import Cadwyn
from cadwyn.structure.versions import Version, VersionBundle

router = APIRouter(prefix="/v1")


@router.get("/")
def homepage():
    return Response("Hello, world", media_type="text/plain")


@router.get("/users/{username}/{page}")
def users_api(username: str, page: int):
    return JSONResponse(
        {"users": [{"username": username, "page": page}]},
    )


@router.get("/users")
def users():
    return Response("All users", media_type="text/plain")


@router.get("/doggies/{dogname}")
def doggies_api(dogname: str):
    return JSONResponse({"doggies": [{"dogname": dogname}]})


mixed_hosts_app = Cadwyn(
    versions=VersionBundle(
        Version("2027-11-15"),
        Version("2022-04-14"),
        Version("2022-03-12"),
        Version("2022-02-11"),
        Version("2022-01-10"),
        Version("1998-11-15"),
    )
)
mixed_hosts_app.generate_and_include_versioned_routers(router)
# Cadwyn generates the versioned routes when the app starts, and the tests inspect them without starting the app
with TestClient(mixed_hosts_app):
    pass
