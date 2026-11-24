"""`cadwyn_core.structure.versions._migrations` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.structure.versions import _migrations

if TYPE_CHECKING:
    from cadwyn_core.structure.versions._migrations import *  # noqa: F403

sys.modules[__name__] = _migrations
