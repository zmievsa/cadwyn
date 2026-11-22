"""`cadwyn_core.structure.enums` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.structure import enums

if TYPE_CHECKING:
    from cadwyn_core.structure.enums import *  # noqa: F403

sys.modules[__name__] = enums
