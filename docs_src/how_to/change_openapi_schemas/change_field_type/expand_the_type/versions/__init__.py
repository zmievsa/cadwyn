from cadwyn import HeadVersion, Version, VersionBundle

from .v2001_01_01 import AddModeratorRoleToUser

version_bundle = VersionBundle(
    HeadVersion(),
    Version("2001-01-01", AddModeratorRoleToUser),
    Version("2000-01-01"),
)
