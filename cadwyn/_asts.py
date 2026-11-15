"""`cadwyn_core._asts` under its old name, as the same module object, so imports and patches of either name work."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core import _asts

if TYPE_CHECKING:
    # Type checkers don't see the module that replaces this one in `sys.modules`
    from cadwyn_core._asts import *  # noqa: F403

sys.modules[__name__] = _asts
