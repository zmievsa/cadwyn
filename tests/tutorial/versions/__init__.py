from datetime import date

from cadwyn.structure import Version, VersionBundle
from cadwyn.structure.versions import HeadVersion
from tests.tutorial.data import head
from tests.tutorial.versions.v2001_1_1 import ChangeAddressToList
from tests.tutorial.versions.v2002_1_1 import ChangeAddressesToSubresource, RemoveAddressesToCreateFromLatest

version_bundle = VersionBundle(
    HeadVersion(RemoveAddressesToCreateFromLatest),
    Version(date(2002, 1, 1), ChangeAddressesToSubresource),
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
    head_schemas_package=head,
)
