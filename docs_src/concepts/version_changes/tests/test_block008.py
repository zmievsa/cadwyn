import sys

import pytest
from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.fake_modules import users
from docs_src.concepts.version_changes.fake_modules.users import BaseUser

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "users", users)
    from docs_src.concepts.version_changes.block008 import (
        ReplaceUserAddressesWithAddress,
    )

database: list[BaseUser] = []
router = VersionedAPIRouter()


@router.post("/users", response_model=BaseUser)
async def create_user(user: BaseUser):
    database.append(user)
    return user


app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", ReplaceUserAddressesWithAddress),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


def test__poor_migration__when_old_client_saves_three_addresses__should_save_and_return_only_the_first_one():
    database.clear()
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.post(
        "/users", json={"addresses": ["1 Main St", "2 Main St", "3 Main St"]}
    )

    assert response.status_code == 200, response.json()
    assert response.json() == {"addresses": ["1 Main St"]}
    assert database == [BaseUser(address="1 Main St")]
