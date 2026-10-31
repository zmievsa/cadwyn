"""Check that the line ranges that the concept pages include cover whole statements.

The pages show parts of the example files with `{! path [ln:1,6-13] !}`, so
adding or removing a line in an example file could cut off the code on a page.
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[3]
PAGES = [
    "api_version_parameter",
    "endpoint_migrations",
    "main_app",
    "schema_generation",
    "schema_migrations",
]
INCLUDE = re.compile(r"\{!>? \./(?P<path>\S+) \[ln:(?P<ranges>[\d,-]+)\] !\}")


def _parse_ranges(ranges: str) -> list[tuple[int, int]]:
    parsed = []
    for part in ranges.split(","):
        start, _, end = part.partition("-")
        parsed.append((int(start), int(end or start)))
    return parsed


INCLUDES = [
    pytest.param(
        REPO_ROOT / match["path"],
        start,
        end,
        id=f"{page}:{match['path']}:{start}-{end}",
    )
    for page in PAGES
    for match in INCLUDE.finditer(
        (REPO_ROOT / f"docs/concepts/{page}.md").read_text()
    )
    for start, end in _parse_ranges(match["ranges"])
]


def test__page_includes__should_have_line_ranges():
    assert INCLUDES


@pytest.mark.parametrize(("path", "start", "end"), INCLUDES)
def test__page_includes__with_line_range__should_include_whole_statements(
    path: Path, start: int, end: int
):
    statements = [
        (
            min(
                [node.lineno]
                + [d.lineno for d in getattr(node, "decorator_list", [])]
            ),
            node.end_lineno or node.lineno,
        )
        for node in ast.parse(path.read_text()).body
    ]
    included = [(s, e) for s, e in statements if start <= s and e <= end]
    cut = [
        (s, e)
        for s, e in statements
        if s <= end and e >= start and (s, e) not in included
    ]

    assert included, "The range does not include any statements"
    assert not cut, f"The range cuts the statements on lines {cut}"
