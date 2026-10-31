# versions/__init__.py

from cadwyn import HeadVersion, Version, VersionBundle

from .v2023_02_10 import RemoveTaxIdEndpoints

versions = VersionBundle(
    HeadVersion(),
    Version("2023-02-10", RemoveTaxIdEndpoints),
    Version("2022-11-16"),
)
