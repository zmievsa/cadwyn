import sys

import pytest
from cadwyn import Cadwyn, Version, VersionBundle
from fastapi.testclient import TestClient

from docs_src.concepts.version_changes.fake_modules import users

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "users", users)
    from docs_src.concepts.version_changes.block009 import router
    from docs_src.concepts.version_changes.block010 import (
        ReplaceUserAddressesWithAddress,
    )

app = Cadwyn(
    versions=VersionBundle(
        Version("2023-02-10", ReplaceUserAddressesWithAddress),
        Version("2022-11-16"),
    )
)
app.generate_and_include_versioned_routers(router)


@pytest.mark.parametrize(
    ("user_id", "expected_body"),
    [
        (1, {"addresses": ["1 Main St", "2 Main St"]}),
        (2, {"addresses": []}),
    ],
)
def test__replace_user_addresses_with_address__when_old_client_gets_user__should_return_all_addresses(
    user_id: int,
    expected_body: dict[str, object],
):
    client = TestClient(app, headers={"x-api-version": "2022-11-16"})

    response = client.get(f"/users/{user_id}")

    assert response.status_code == 200, response.json()
    assert response.json() == expected_body


@pytest.mark.parametrize(
    ("user_id", "expected_body"),
    [(1, {"address": "1 Main St"}), (2, {"address": None})],
)
def test__replace_user_addresses_with_address__when_new_client_gets_user__should_return_the_first_address(
    user_id: int,
    expected_body: dict[str, object],
):
    client = TestClient(app, headers={"x-api-version": "2023-02-10"})

    response = client.get(f"/users/{user_id}")

    assert response.status_code == 200, response.json()
    assert response.json() == expected_body
