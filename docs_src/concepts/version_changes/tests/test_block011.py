import sys

import pytest

from docs_src.concepts.version_changes.fake_modules import users, versions
from docs_src.concepts.version_changes.fake_modules.users import UserResource

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "users", users)
    monkeypatch.setitem(sys.modules, "versions", versions)
    from docs_src.concepts.version_changes.block011 import (
        response_from_2000_01_01,
    )


def test__migrate_response__to_2000_01_01__should_run_migrations_and_return_the_versioned_model():
    assert type(response_from_2000_01_01.body).__name__ == "UserResource"
    assert type(response_from_2000_01_01.body) is not UserResource
    assert response_from_2000_01_01.body.model_dump() == {"full_name": "John"}
    assert response_from_2000_01_01.status_code == 200
