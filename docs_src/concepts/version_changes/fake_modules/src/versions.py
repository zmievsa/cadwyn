from cadwyn import Version, VersionBundle

from ...block015 import RejectUserAddressesMissingFromExternalService

versions = VersionBundle(
    Version("2022-11-11", RejectUserAddressesMissingFromExternalService),
    Version("2022-10-01"),
)

__all__ = ["RejectUserAddressesMissingFromExternalService", "versions"]
