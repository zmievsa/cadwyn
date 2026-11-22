"""`cadwyn_core.schema_generation._callables` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.schema_generation import _callables

if TYPE_CHECKING:
    from cadwyn_core.schema_generation._callables import *  # noqa: F403

sys.modules[__name__] = _callables
