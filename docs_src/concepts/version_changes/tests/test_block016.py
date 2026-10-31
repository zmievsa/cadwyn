import asyncio
import sys
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from docs_src.concepts.version_changes.fake_modules import src
from docs_src.concepts.version_changes.fake_modules.src import versions

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "src", src)
    monkeypatch.setitem(sys.modules, "src.versions", versions)
    from docs_src.concepts.version_changes.block016 import create_user


@pytest.fixture
def api_version(request: pytest.FixtureRequest) -> Iterator[None]:
    token = versions.versions.api_version_var.set(request.param)
    yield
    versions.versions.api_version_var.reset(token)


@pytest.mark.parametrize("api_version", ["2022-10-01"], indirect=True)
def test__create_user__before_the_side_effect__should_skip_the_address_check(
    api_version: None,
):
    payload = SimpleNamespace(address="Unknown St")

    asyncio.run(create_user(payload))


@pytest.mark.parametrize("api_version", ["2022-11-11", None], indirect=True)
def test__create_user__with_the_side_effect_applied__should_reject_unknown_addresses(
    api_version: None,
):
    payload = SimpleNamespace(address="Unknown St")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_user(payload))

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("api_version", ["2022-11-11"], indirect=True)
def test__create_user__with_the_side_effect_applied__should_accept_known_addresses(
    api_version: None,
):
    payload = SimpleNamespace(address="1 Main St")

    asyncio.run(create_user(payload))
