"""`cadwyn_core.schema_generation._generics` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.schema_generation import _generics

if TYPE_CHECKING:
    from cadwyn_core.schema_generation._generics import *  # noqa: F403

sys.modules[__name__] = _generics
