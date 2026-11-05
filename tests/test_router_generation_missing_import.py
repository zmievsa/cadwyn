"""The test is kept in a separate file to avoid accidental importing of Literal"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated  # Note: _no_ runtime import of Literal. This triggers the bug!

if TYPE_CHECKING:
    from typing import Literal

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from cadwyn import current_dependency_solver
from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle, VersionChange
from tests._resources.utils import PYDANTIC_RAISES_RECURSION_ERROR_ON_DEEPCOPY


def _make_apps():
    class MySchema(BaseModel):
        foo: str = Field(coerce_numbers_to_str=True)

    class MyVersionChange(VersionChange):
        description = "Hello"
        instructions_to_migrate_to_previous_version = (
            schema(MySchema).field("foo").had(type=int),
            schema(MySchema).field("foo").didnt_have("coerce_numbers_to_str"),
        )

    async def dep_with_solver(
        dependency_solver: Annotated[Literal["fastapi", "cadwyn"], Depends(current_dependency_solver)],
    ):
        pass  # pragma: no cover

    async def route_with_inner_schema_forwardref(dep: MySchema) -> MySchema:
        return dep  # pragma: no cover

    app = Cadwyn(versions=VersionBundle(Version("2001-01-01", MyVersionChange), Version("2000-01-01")))
    router = VersionedAPIRouter()
    router.post("/test", dependencies=[Depends(dep_with_solver)])(route_with_inner_schema_forwardref)
    app.generate_and_include_versioned_routers(router)

    fastapi_app = FastAPI()
    fastapi_app.post("/test", dependencies=[Depends(dep_with_solver)])(route_with_inner_schema_forwardref)
    return app, fastapi_app


@pytest.mark.skipif(not PYDANTIC_RAISES_RECURSION_ERROR_ON_DEEPCOPY, reason="Fixed in pydantic 2.14.3")
def test__missing_an_import_used_in_annotations_with_from_future_import_annotations():
    """Regression test for https://github.com/zmievsa/cadwyn/issues/324"""
    app, _ = _make_apps()
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    with pytest.raises(
        ImportError,
        match=re.escape(
            "You are likely missing an import from typing such as typing.Literal which causes RecursionError"
        ),
    ):
        client_2000.post("/test", json={"foo": 1})


# The lock pins an older pydantic so only the daily tests with the newest dependencies run this test
@pytest.mark.skipif(PYDANTIC_RAISES_RECURSION_ERROR_ON_DEEPCOPY, reason="Needs pydantic 2.14.3 or later")
def test__missing_an_import_used_in_annotations__behaves_like_fastapi():  # pragma: no cover
    """Without the RecursionError, Cadwyn handles unresolved annotations the same way FastAPI does"""
    app, fastapi_app = _make_apps()
    expected = TestClient(fastapi_app).post("/test", json={"foo": 1})
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        response = client.post("/test", json={"foo": 1})
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())
