import functools
from typing import Annotated, Literal

from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi import Depends
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.block014 import my_dependency

solver_calls: list[str] = []


# Keeps the signature of 'my_dependency', so FastAPI resolves its dependencies
@functools.wraps(my_dependency)
def recorded_dependency(dependency_solver: Literal["fastapi", "cadwyn"]):
    solver_calls.append(dependency_solver)
    return my_dependency(dependency_solver)


router = VersionedAPIRouter()


@router.get("/users")
async def list_users(_: Annotated[None, Depends(recorded_dependency)]):
    return []


app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))
app.generate_and_include_versioned_routers(router)


def test__my_dependency__in_versioned_request__should_run_before_and_after_migration():
    solver_calls.clear()
    client = TestClient(app, headers={"x-api-version": "2000-01-01"})

    response = client.get("/users")

    assert response.status_code == 200, response.json()
    assert solver_calls == ["fastapi", "cadwyn"]
