import runpy
import sys

import pytest

from docs_src.concepts.version_changes.fake_modules import users


def test__root_model__subclassed_twice__should_give_two_different_classes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setitem(sys.modules, "users", users)

    runpy.run_module(
        "docs_src.concepts.version_changes.block013", alter_sys=True
    )

    assert capsys.readouterr().out == "False\n"
