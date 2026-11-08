from collections import defaultdict
from typing import Any, NamedTuple

from pydantic import BaseModel, Field

from cadwyn import Cadwyn, Version, VersionBundle


class Point(NamedTuple):
    x: int
    y: int


class Named(BaseModel):
    point: Point = Field(default=Point(1, 2))


class DefaultDict(BaseModel):
    counts: dict[str, int] = Field(default=defaultdict(int, a=1))


class SetFormattingDefaults(BaseModel):
    empty: Any = frozenset()
    items: Any = {1, 2, 3}
    frozen: Any = frozenset({1, 2, 3})


app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))
