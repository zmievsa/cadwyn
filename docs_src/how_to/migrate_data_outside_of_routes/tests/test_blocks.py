import sys

import pytest

from docs_src.how_to.migrate_data_outside_of_routes import versions
from docs_src.how_to.migrate_data_outside_of_routes.versions import User

with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setitem(sys.modules, "versions", versions)
    from docs_src.how_to.migrate_data_outside_of_routes import (
        block001,
        block002,
        block003,
    )


def test__migrate_response__instead_of_migrate_response_body__should_give_the_same_body():
    assert block002.body == block001.body
    assert block002.body.model_dump() == {"name": "John"}


def test__migrate_request__for_message_of_older_version__should_give_the_head_model():
    assert block003.user == User(full_name="John")
