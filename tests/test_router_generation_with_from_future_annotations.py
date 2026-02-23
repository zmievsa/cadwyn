from __future__ import annotations

import sys
from typing import Annotated
from unittest.mock import patch

import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, WithJsonSchema

from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle, VersionChange


class OuterSchema(BaseModel):
    bar: MySchema

    extra_annotated: Annotated[str, WithJsonSchema({"type": "string", "description": "Hello"})] = ""


class MySchema(BaseModel):
    foo: str = Field(coerce_numbers_to_str=True)


class MyVersionChange(VersionChange):
    description = "Hello"
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(type=int),
        schema(MySchema).field("foo").didnt_have("coerce_numbers_to_str"),
    )


app = Cadwyn(versions=VersionBundle(Version("2001-01-01", MyVersionChange), Version("2000-01-01")))
router = VersionedAPIRouter()


@router.post("/test")
async def route_with_inner_schema_forwardref(dep: MySchema) -> MySchema:
    return dep


@router.post("/test2")
async def route_with_outer_schema_forwardref(dep: OuterSchema) -> OuterSchema:
    return dep


app.generate_and_include_versioned_routers(router)


def test__router_generation__using_forwardref_inner_global_schema_in_body():
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test", json={"foo": 1}).json() == {"foo": 1}
    assert client_2001.post("/test", json={"foo": 1}).json() == {"foo": "1"}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == 200
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == 200


def test__router_generation__using_forwardref_outer_global_schema_in_body():
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test2", json={"bar": {"foo": 1}}).json() == {"bar": {"foo": 1}, "extra_annotated": ""}
    assert client_2001.post("/test2", json={"bar": {"foo": 1}}).json() == {"bar": {"foo": "1"}, "extra_annotated": ""}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == 200
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == 200


class CallableClassDependency:
    """A callable class to be used as a dependency (with forward ref annotation due to future annotations)."""

    def __init__(self, label: str):
        super().__init__()
        self.label = label

    async def __call__(self, request: Request) -> None:
        pass


def test__router_generation__using_callable_class_dependency_with_forwardref():
    """Test that callable class instances work as dependencies with future annotations.

    Regression test for https://github.com/zmievsa/cadwyn/issues/321
    """

    class EmptyVersionChange(VersionChange):
        description = "Empty version change"
        instructions_to_migrate_to_previous_version = ()

    test_router = VersionedAPIRouter()

    @test_router.get("/run", dependencies=[Depends(CallableClassDependency("route-level"))])
    async def run():
        return {"status": "ok"}

    test_app = Cadwyn(versions=VersionBundle(Version("2001-01-01", EmptyVersionChange), Version("2000-01-01")))
    test_app.generate_and_include_versioned_routers(test_router)

    client = TestClient(test_app, headers={test_app.router.api_version_parameter_name: "2000-01-01"})
    response = client.get("/run")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test__missing_an_import():
    """Regression test for https://github.com/zmievsa/cadwyn/issues/324"""
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    with patch.dict(sys.modules, {"Annotated": None}):
        pytest.raises(
            ImportError,
            match="You are likely missing an import from typing such as typing.Literal which causes RecursionError",
        )
        client_2000.post("/test", json={"foo": 1}).json()
