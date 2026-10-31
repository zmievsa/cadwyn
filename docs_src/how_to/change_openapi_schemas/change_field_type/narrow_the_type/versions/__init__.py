from cadwyn import HeadVersion, Version, VersionBundle

from .v2001_01_01 import (
    ChangeUserDateOfBirthFromDatetimeToDate,
    ChangeUserDateOfBirthFromDatetimeToDateInLatest,
)

version_bundle = VersionBundle(
    HeadVersion(ChangeUserDateOfBirthFromDatetimeToDateInLatest),
    Version("2001-01-01", ChangeUserDateOfBirthFromDatetimeToDate),
    Version("2000-01-01"),
)
