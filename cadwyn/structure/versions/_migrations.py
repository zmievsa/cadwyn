"""`cadwyn_core.structure.versions._migrations` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _migrations

sys.modules[__name__] = _migrations
