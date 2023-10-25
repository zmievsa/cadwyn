from contextvars import ContextVar
from datetime import date

from cadwyn.structure import Version, VersionBundle
from tests.test_tutorial.test_users_example002.versions.v2001_1_1 import ChangeAddressToList
from tests.test_tutorial.test_users_example002.versions.v2002_1_1 import ChangeAddressesToSubresource

version_bundle = VersionBundle(
    Version(date(2002, 1, 1), ChangeAddressesToSubresource),
    Version(date(2001, 1, 1), ChangeAddressToList),
    Version(date(2000, 1, 1)),
    api_version_var=ContextVar("cadwyn_api_version"),
)
