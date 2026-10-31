import sys
from pathlib import Path

import pytest

from docs_src.how_to.change_openapi_schemas.example_project import (
    example_project_client,
)

EXAMPLES_DIR = Path(__file__).parent.parent
FIRST_EXAMPLE_DIR = EXAMPLES_DIR / "add_field/required_with_compatible_default"
SECOND_EXAMPLE_DIR = (
    EXAMPLES_DIR / "rename_a_field_in_schema/rename_summary_to_bio"
)


def test__example_project_client__when_another_example_is_imported__should_raise_runtime_error():
    with (
        example_project_client(FIRST_EXAMPLE_DIR),
        pytest.raises(RuntimeError, match="already imported"),
        example_project_client(SECOND_EXAMPLE_DIR),
    ):
        pass  # pragma: no cover


def test__example_project_client__after_exit__should_forget_the_example_modules():
    with example_project_client(FIRST_EXAMPLE_DIR):
        assert {"main", "users", "versions"} <= sys.modules.keys()

    assert not {"main", "users", "versions"} & sys.modules.keys()
    assert str(FIRST_EXAMPLE_DIR.resolve()) not in sys.path
