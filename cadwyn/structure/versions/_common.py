"""`cadwyn_core.structure.versions._common` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _common

sys.modules[__name__] = _common
