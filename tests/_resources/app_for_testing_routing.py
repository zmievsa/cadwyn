from datetime import date

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from cadwyn import Cadwyn
from cadwyn.structure.versions import Version, VersionBundle

router = APIRouter(prefix="/v1")


@router.get("/")
def homepage():
    return Response("Hello, world", media_type="text/plain")


@router.get("/users/{username}/{page}")
def users_api(request: Request):
    return JSONResponse(
        {"users": [{"username": request.path_params["username"], "page": int(request.path_params["page"])}]},
    )


@router.get("/users")
def users():
    return Response("All users", media_type="text/plain")


@router.get("/doggies/{dogname}")
def doggies_api(request: Request):
    return JSONResponse({"doggies": [{"dogname": request.path_params["dogname"]}]})


versions = [
    "2022-01-10",
    "2022-02-11",
    "1998-11-15",
    "2022-03-12",
    "2027-11-15",
    "2022-04-14",
]
mixed_hosts_app = Cadwyn(versions=VersionBundle(Version(date(1998, 11, 15))))
for version in versions:
    mixed_hosts_app.add_header_versioned_routers(
        router,
        header_value=version,
    )
