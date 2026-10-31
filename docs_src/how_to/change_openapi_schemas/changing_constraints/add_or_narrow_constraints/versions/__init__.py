from cadwyn import HeadVersion, Version, VersionBundle

from .v2001_01_01 import (
    LimitUserNamesTo250Characters,
    LimitUserNamesTo250CharactersInLatest,
)

version_bundle = VersionBundle(
    HeadVersion(LimitUserNamesTo250CharactersInLatest),
    Version("2001-01-01", LimitUserNamesTo250Characters),
    Version("2000-01-01"),
)
