import runpy
import sys

import pytest

from docs_src.concepts.version_changes.fake_modules import users


def test__root_model__parametrized_twice_with_the_same_type__should_be_the_same_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setitem(sys.modules, "users", users)

    runpy.run_module(
        "docs_src.concepts.version_changes.block012", alter_sys=True
    )

    assert capsys.readouterr().out == "True\n"
