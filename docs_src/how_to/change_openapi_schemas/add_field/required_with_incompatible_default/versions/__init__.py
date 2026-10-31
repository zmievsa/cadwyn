from cadwyn import HeadVersion, Version, VersionBundle

from .v2001_01_01 import (
    AddRequiredPhoneToUser,
    MakeUserPhoneNonNullableInLatest,
)

version_bundle = VersionBundle(
    HeadVersion(MakeUserPhoneNonNullableInLatest),
    Version("2001-01-01", AddRequiredPhoneToUser),
    Version("2000-01-01"),
)
