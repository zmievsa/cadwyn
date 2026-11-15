"""`cadwyn_core.structure.versions._core_schema` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.structure.versions import _core_schema

if TYPE_CHECKING:
    # Type checkers don't see the module that replaces this one in `sys.modules`
    from cadwyn_core.structure.versions._core_schema import *  # noqa: F403

sys.modules[__name__] = _core_schema
