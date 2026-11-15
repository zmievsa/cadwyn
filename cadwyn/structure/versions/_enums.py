"""`cadwyn_core.structure.versions._enums` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.structure.versions import _enums

if TYPE_CHECKING:
    # Type checkers don't see the module that replaces this one in `sys.modules`
    from cadwyn_core.structure.versions._enums import *  # noqa: F403

sys.modules[__name__] = _enums
