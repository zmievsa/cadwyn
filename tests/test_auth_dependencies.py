from typing import NoReturn

import pytest
from fastapi import APIRouter, HTTPException, Security
from fastapi.testclient import TestClient

from cadwyn.applications import Cadwyn
from cadwyn.structure.versions import Version, VersionBundle


class ScarySecurity:
    """It's IMPORTANT that we use an instance of a class instead of a function because it can be properly copied.

    It's an edge case.
    """

    async def __call__(self) -> NoReturn:
        raise HTTPException(status_code=401, detail="Unauthorized")


scary_security = ScarySecurity()


@pytest.fixture
def cadwyn_app():
    router = APIRouter()

    @router.get("/hello")
    async def world(user=Security(scary_security)):
        return {"hello": "world", "user": user}

    app = Cadwyn(versions=VersionBundle(Version("2023-11-16")))
    app.include_router(router)
    app.generate_and_include_versioned_routers(router)

    return app


@pytest.fixture
def user_client(cadwyn_app: Cadwyn):
    with TestClient(app=cadwyn_app, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def _override_auth_dependency(cadwyn_app: Cadwyn) -> None:
    cadwyn_app.dependency_overrides[scary_security] = lambda: {"id": 123}


def test__no_dependency_overrides_with_unversioned_routes(user_client: TestClient):
    response = user_client.get("/hello")
    assert response.status_code == 401, response.json()


def test__no_dependency_overrides_with_versioned_routes(user_client: TestClient):
    response = user_client.get("/hello", headers={"x-api-version": "2023-11-16"})
    assert response.status_code == 401, response.json()


def test__dependency_overrides_with_unversioned_routes(user_client: TestClient, _override_auth_dependency: None):
    response = user_client.get("/hello")
    assert response.status_code == 200, response.json()


def test__dependency_overrides_with_versioned_routes(user_client: TestClient, _override_auth_dependency: None):
    response = user_client.get("/hello", headers={"x-api-version": "2023-11-16"})
    assert response.status_code == 200, response.json()
