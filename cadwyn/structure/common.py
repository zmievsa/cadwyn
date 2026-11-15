"""`cadwyn_core.structure.common` under its old name, as the same module object."""

import sys

from cadwyn_core.structure import common

sys.modules[__name__] = common
