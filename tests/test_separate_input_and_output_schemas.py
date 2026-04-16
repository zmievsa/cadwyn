# pyright: standard
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict
from cadwyn import Cadwyn
from cadwyn import VersionedAPIRouter
from fastapi import APIRouter, FastAPI, Response
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse, RedirectResponse

from cadwyn.structure.versions import Version, VersionBundle

versioned_router = VersionedAPIRouter()


class Item(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    name: str
    description: str | None = None




@versioned_router.post("/items/")
def create_item(item: Item):
    return item


@versioned_router.get("/items/")
def read_items() -> list[Item]:
    return [
        Item(
            name="Portal Gun",
            description="Device to travel through the multi-rick-verse",
        ),
        Item(name="Plumbus"),
    ]

app = Cadwyn(
    versions=VersionBundle(Version("2021-01-01")),
    separate_input_output_schemas=False,
)

app.generate_and_include_versioned_routers(versioned_router)

def test_separate_input_output_schemas():
    with TestClient(app) as client:
        resp = client.get("/openapi.json?version=2021-01-01").json()
        assert "Item" in resp.get("components", {}).get("schemas", {})
