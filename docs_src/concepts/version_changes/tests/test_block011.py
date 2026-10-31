import sys

import pytest

from docs_src.concepts.version_changes.fake_modules import users, versions
from docs_src.concepts.version_changes.fake_modules.users import UserResource

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "users", users)
    monkeypatch.setitem(sys.modules, "versions", versions)
    from docs_src.concepts.version_changes.block011 import (
        body_from_2000_01_01,
    )


def test__migrate_response_body__to_2000_01_01__should_run_converters_and_wrap_in_the_versioned_model():
    assert type(body_from_2000_01_01).__name__ == "UserResource"
    assert type(body_from_2000_01_01) is not UserResource
    assert body_from_2000_01_01.model_dump() == {"full_name": "John"}
