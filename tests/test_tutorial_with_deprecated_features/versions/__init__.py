from datetime import date

from cadwyn.structure import Version, VersionBundle

from ..data import latest
from .v2001_1_1 import ChangeAddressToList
from .v2002_1_1 import ChangeAddressesToSubresource

version_bundle = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressesToSubresource),
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
    latest_schemas_package=latest,
)
