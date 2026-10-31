"""Run the `cadwyn render` commands of the page the way a user runs them.

The commands run from the directory of the app, like the page describes, so
they import `data.schemas` and `main` from it.
"""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

PAGE_DIR = Path(__file__).parents[1]
PROJECT_DIR = PAGE_DIR / "render_project"


def _run_command_from(block: str) -> list[str]:
    [command] = (PAGE_DIR / block).read_text().splitlines()
    executable, *args = shlex.split(command)
    # The `cadwyn` console script lives next to the Python of the project venv
    # (in bin/ or, on Windows, in Scripts/).
    console_script = shutil.which(executable, path=Path(sys.executable).parent)
    assert console_script is not None
    result = subprocess.run(
        [console_script, *args],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "120"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Without --raw, the output has line numbers: "  1 from pydantic import ..."
    code = [
        line.lstrip().partition(" ")[2].rstrip()
        for line in result.stdout.splitlines()
    ]
    return [line for line in code if line]


def test__render_module__from_app_directory__should_print_module_schemas_in_that_version():
    assert _run_command_from("block003.sh") == [
        "from pydantic import BaseModel",
        "class UserCreateRequest(BaseModel):",
        "    name: str = Field()",
        "class UserResource(BaseModel):",
        "    id: int = Field()",
        "    name: str = Field()",
    ]


def test__render_model__from_app_directory__should_print_schema_in_that_version():
    assert _run_command_from("block004.sh") == [
        "class UserCreateRequest(BaseModel):",
        "    name: str = Field()",
    ]
