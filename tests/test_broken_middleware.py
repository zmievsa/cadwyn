import datetime

from fastapi import APIRouter
from cadwyn import Cadwyn, VersionBundle
from cadwyn.structure import Version


app = Cadwyn(versions=VersionBundle(Version(datetime.date(2024, 1, 2))))

router = APIRouter()


@router.get("/")
def hello():
    return "hello"

app.add_header_versioned_routers(router, header_value="2024-01-02")
