from datetime import date

from cadwyn.main import Cadwyn
from cadwyn.structure import Version, VersionBundle
from tests.test_tutorial.data import latest
from tests.test_tutorial.versions.v2001_1_1 import ChangeAddressToList
from tests.test_tutorial.versions.v2002_1_1 import ChangeAddressesToSubresource

version_bundle = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressesToSubresource),
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
)

app = Cadwyn(latest_schemas_module=latest, versions=version_bundle)
