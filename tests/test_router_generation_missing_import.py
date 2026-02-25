"""The test is kept in a separate file to avoid accidental importing of Literal"""

from __future__ import annotations

from typing import Annotated  # Note: _no_ import of Literal. This triggers the bug!

import fastapi
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import cadwyn
from cadwyn import current_dependency_solver
from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle, VersionChange


def test__missing_an_import_used_in_annotations_with_from_future_import_annotations():
    """Regression test for https://github.com/zmievsa/cadwyn/issues/324"""

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

    async def dep_with_solver(
        dependency_solver: Annotated[Literal[fastapi, cadwyn], Depends(current_dependency_solver)],  # noqa: F821
    ):
        pass

    @router.post(
        "/test",
        dependencies=[Depends(dep_with_solver)],
    )
    async def route_with_inner_schema_forwardref(dep: MySchema) -> MySchema:
        return dep

    app.generate_and_include_versioned_routers(router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    with pytest.raises(
        ImportError,
        match="You are likely missing an import from typing such as typing.Literal which causes RecursionError",
    ):
        assert client_2000.post("/test", json={"foo": 1}).json() == {"foo": 1}
