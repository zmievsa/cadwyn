"""`cadwyn_core.structure.versions._migration_targets` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _migration_targets

sys.modules[__name__] = _migration_targets
