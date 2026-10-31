# versions/__init__.py

from cadwyn import HeadVersion, Version, VersionBundle

from .v2001_01_01 import (
    RemoveMiddleNameFromUser,
    RemoveUserMiddleNameFromLatestVersion,
)

version_bundle = VersionBundle(
    HeadVersion(RemoveUserMiddleNameFromLatestVersion),
    Version("2001-01-01", RemoveMiddleNameFromUser),
    Version("2000-01-01"),
)
