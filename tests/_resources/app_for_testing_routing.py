import pytest
from fastapi import APIRouter
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


versions = [
    "2022-01-10",
    "2022-02-11",
    "1998-11-15",
    "2022-03-12",
    "2027-11-15",
    "2022-04-14",
]
mixed_hosts_app = Cadwyn(versions=VersionBundle(Version("1998-11-15")))
for version in versions:
    with pytest.warns(DeprecationWarning):
        mixed_hosts_app.add_header_versioned_routers(  # pyright: ignore[reportDeprecated]
            router,
            header_value=version,
        )
