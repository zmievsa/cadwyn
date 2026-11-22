"""Fail when a file has more comments than `comment_baseline.json` allows.

Cadwyn keeps no comments in its code: the reasons behind surprising decisions live in
`docs/theory/design_decisions.md`. The baseline lists the files that still have comments and how many,
and it may only shrink. Tool directives (`# pragma: no cover`, `# noqa`, `# ty: ignore`, and the like),
shebangs, the help texts that `make help` reads, and the version labels that Dependabot reads after
pinned actions don't count. Neither do the documentation examples in `docs_src/`, whose comments are
part of the docs.

Run `uv run --no-project scripts/check_comments.py --update` after you remove comments.
"""

import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).with_name("comment_baseline.json")
EXEMPT_PREFIXES = ("docs_src/",)
HASH_SUFFIXES = {".toml", ".yaml", ".yml", ".ini", ".cfg", ".sh"}
HASH_NAMES = {"Makefile", ".gitignore"}
MARKUP_SUFFIXES = {".md", ".html"}
SCRIPT_SUFFIXES = {".js", ".css"}
DIRECTIVE = re.compile(
    r"#\s*(?:"
    r"pragma:\s*no\s*(?:cover|branch)"
    r"|noqa(?::\s*[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)?"
    r"|ty:\s*ignore(?:\[[^\]]*\])?"
    r"|type:\s*ignore(?:\[[^\]]*\])?"
    r"|fmt:\s*(?:off|on|skip)"
    r"|isort:\s*(?:split|skip|skip_file|off|on)"
    r"|ruff:\s*noqa\b(?::\s*[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)?"
    r")",
    re.IGNORECASE,
)
VERSION_LABEL = re.compile(r"#\s*v?\d+(?:\.\d+)+\s*$")
MAKE_HELP = re.compile(r"^[a-zA-Z0-9_-]+:.*?##")


def is_prose(comment: str) -> bool:
    return bool(re.sub(r"[\s#-]+", "", DIRECTIVE.sub("", comment)))


def python_comments(text: str) -> list[tuple[int, str]]:
    found = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        if token.start[0] == 1 and token.string.startswith("#!"):
            continue
        if is_prose(token.string):
            found.append((token.start[0], token.string))
    return found


def hash_comment_start(line: str) -> int | None:
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return index
    return None


def hash_comments(text: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        start = hash_comment_start(line)
        if start is None or (number == 1 and line.startswith("#!")) or MAKE_HELP.match(line):
            continue
        comment = line[start:]
        if VERSION_LABEL.fullmatch(comment) and "uses:" in line[:start]:
            continue
        if is_prose(comment):
            found.append((number, comment))
    return found


def markup_comments(text: str) -> list[tuple[int, str]]:
    return [(text.count("\n", 0, match.start()) + 1, match.group(0)) for match in re.finditer(r"<!--.*?-->", text, re.S)]


def script_comments(text: str) -> list[tuple[int, str]]:
    return [
        (text.count("\n", 0, match.start()) + 1, match.group(0))
        for match in re.finditer(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", text, re.S)
    ]


def comments_in(path: str) -> list[tuple[int, str]]:
    file = ROOT / path
    if path.startswith(EXEMPT_PREFIXES) or not file.is_file():
        return []
    readers = []
    if file.suffix == ".py":
        readers.append(python_comments)
    if file.suffix in HASH_SUFFIXES or file.name in HASH_NAMES:
        readers.append(hash_comments)
    if file.suffix in MARKUP_SUFFIXES:
        readers.append(markup_comments)
    if file.suffix in SCRIPT_SUFFIXES or file.suffix == ".html":
        readers.append(script_comments)
    if not readers:
        return []
    text = file.read_text(encoding="utf-8")
    return sorted(comment for reader in readers for comment in reader(text))


def tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(set(output.splitlines()))


def main() -> int:
    found = {path: comments for path in tracked_files() if (comments := comments_in(path))}
    counts = {path: len(comments) for path, comments in found.items()}
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    status = 0
    for path, comments in found.items():
        allowed = baseline.get(path, 0)
        if len(comments) > allowed:
            status = 1
            print(f"{path}: {len(comments)} comments, and the baseline allows {allowed}:")
            for number, comment in comments:
                print(f"  {path}:{number}: {comment.strip()}")
    shrunk = sorted(path for path, allowed in baseline.items() if counts.get(path, 0) < allowed)
    if shrunk:
        status = 1
        print("These files have fewer comments than the baseline allows:", *shrunk, sep="\n  ")
        print("Run `uv run --no-project scripts/check_comments.py --update` to shrink the baseline.")
    if status:
        print(
            "Cadwyn keeps no comments in its code. Put the reason for a surprising decision in "
            "docs/theory/design_decisions.md instead (see docs/home/CONTRIBUTING.md)."
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
