"""Check that the line ranges the how-to pages include still cover whole statements.

The pages show parts of the example files with `{!> path [ln:start-end] !}`, so
adding or removing a line in an example file could cut off the code on a page.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[4]
PAGES_DIR = REPO_ROOT / "docs/how_to/change_openapi_schemas"
INCLUDE = re.compile(
    r"\{!>? (?P<path>\S+) \[ln:(?P<start>\d+)-(?P<end>\d+)\] !\}"
)

INCLUDES = [
    pytest.param(
        REPO_ROOT / match["path"],
        int(match["start"]),
        int(match["end"]),
        id=f"{page.name}:{match['path']}:{match['start']}-{match['end']}",
    )
    for page in sorted(PAGES_DIR.glob("*.md"))
    for match in INCLUDE.finditer(page.read_text())
]


def test__page_includes__should_have_line_ranges():
    assert INCLUDES


@pytest.mark.parametrize(("path", "start", "end"), INCLUDES)
def test__page_includes__with_line_range__should_include_whole_statements(
    path: Path, start: int, end: int
):
    lines = path.read_text().splitlines()
    first, last = lines[start - 1], lines[end - 1]
    before = lines[start - 2] if start > 1 else ""
    after = lines[end] if end < len(lines) else ""

    assert first.strip(), "The range starts with a blank line"
    assert not first[0].isspace(), "The range starts inside a statement"
    assert not before.strip() or before.startswith("#"), (
        "The range starts after the beginning of a statement"
    )
    assert last.strip(), "The range ends with a blank line"
    assert not after.strip(), "The range ends before the end of a statement"
