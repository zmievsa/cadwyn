from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Union

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cadwyn import Cadwyn, ResponseInfo, VersionChange, convert_response_to_previous_version_for, schema
from cadwyn.structure.versions import Version, VersionBundle, VersionType
from tests._resources.versioned_app.webhooks import router as unversioned_router

versioned_router = APIRouter(prefix="/v1")


class VersionResponseModel(BaseModel):
    my_version2: int


@versioned_router.get("", response_model=VersionResponseModel)
def read_root():
    return {"my_version2": 2}


@versioned_router.websocket_route("/non_api_route_made_only_to_verify_that_we_can_have_non_api_routes")
def non_api_route_made_only_to_verify_that_we_can_have_non_api_routes():
    raise NotImplementedError


# TODO: Add better tests for covering lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # pragma: no cover


def _create_versioned_app(api_version_var: Union[ContextVar[Union[VersionType, None]], None] = None) -> Cadwyn:
    # A version change can only belong to one version bundle, so every app gets its own
    class ReturnFirstVersionResponse(VersionChange):
        description = "2021-01-01 returns my_version1 instead of my_version2"
        instructions_to_migrate_to_previous_version = (
            schema(VersionResponseModel).field("my_version2").had(name="my_version1"),
        )

        @convert_response_to_previous_version_for(VersionResponseModel)
        def return_first_version_response(response: ResponseInfo) -> None:
            response.body = {"my_version1": 1}

    app = Cadwyn(
        versions=VersionBundle(
            Version("2022-02-02", ReturnFirstVersionResponse),
            Version("2021-01-01"),
            api_version_var=api_version_var,
        ),
        lifespan=lifespan,
    )
    app.generate_and_include_versioned_routers(versioned_router)
    app.include_router(unversioned_router)
    return app


versioned_app = _create_versioned_app()
versioned_app_with_custom_api_version_var = _create_versioned_app(ContextVar("My api version"))

# TODO: We should not have any clients that are run like this. Instead, all of them must run using "with"
client_without_headers = TestClient(versioned_app)
client_without_headers_and_with_custom_api_version_var = TestClient(versioned_app_with_custom_api_version_var)

if __name__ == "__main__":
    uvicorn.run(versioned_app)
