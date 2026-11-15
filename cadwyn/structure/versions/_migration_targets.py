"""`cadwyn_core.structure.versions._migration_targets` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.structure.versions import _migration_targets

if TYPE_CHECKING:
    # Type checkers don't see the module that replaces this one in `sys.modules`
    from cadwyn_core.structure.versions._migration_targets import *  # noqa: F403

sys.modules[__name__] = _migration_targets
