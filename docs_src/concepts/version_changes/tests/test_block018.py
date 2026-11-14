import sys

import pytest

from docs_src.concepts.version_changes.fake_modules import users, versions
from docs_src.concepts.version_changes.fake_modules.users import (
    UserCreateRequest,
)

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "users", users)
    monkeypatch.setitem(sys.modules, "versions", versions)
    from docs_src.concepts.version_changes.block018 import (
        request_body_from_2000_01_01,
    )


def test__migrate_request_body__from_2000_01_01__should_run_migrations_and_return_the_head_model():
    assert request_body_from_2000_01_01 == UserCreateRequest(name="John")
