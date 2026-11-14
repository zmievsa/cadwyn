import copy
import pickle
from collections.abc import Callable
from typing import Any

import pytest

from cadwyn.exceptions import CadwynHeadRequestValidationError


class _SubclassWithItsOwnInitError(CadwynHeadRequestValidationError):
    def __init__(self, errors: list[Any], *, body: Any = None, version: str = "2000-01-01") -> None:
        super().__init__(errors, body, version)


@pytest.mark.parametrize(
    "restore",
    [
        pytest.param(lambda error: pickle.loads(pickle.dumps(error)), id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(
            lambda: CadwynHeadRequestValidationError([{"type": "missing", "loc": ("a",)}], {"b": 1}, "2000-01-01"),
            id="error",
        ),
        pytest.param(
            lambda: _SubclassWithItsOwnInitError([{"type": "missing", "loc": ("a",)}], body={"b": 1}), id="subclass"
        ),
    ],
)
def test__cadwyn_head_request_validation_error__when_pickled_or_copied__should_restore_the_error(
    make_error: Callable[[], CadwynHeadRequestValidationError], restore: Callable[[Any], Any]
):
    error = make_error()
    error.add_note("in worker 42")
    # E.g. an attribute that the code that raises the error sets
    error.__dict__["task_id"] = "42"

    restored = restore(error)

    assert type(restored) is type(error)
    assert (restored.errors, restored.body, restored.version, str(restored), restored.args) == (
        error.errors,
        error.body,
        error.version,
        str(error),
        error.args,
    )
    assert (restored.__notes__, restored.__dict__["task_id"]) == (["in worker 42"], "42")
