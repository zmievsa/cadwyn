from typing import Any

_LEGACY_KEY = "x-legacy"


def add_legacy_marker(json_schema: dict[str, Any]) -> None:
    json_schema[_LEGACY_KEY] = True
