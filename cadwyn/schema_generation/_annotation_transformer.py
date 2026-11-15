"""`cadwyn_core.schema_generation._annotation_transformer` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.schema_generation import _annotation_transformer

if TYPE_CHECKING:
    # Type checkers don't see the module that replaces this one in `sys.modules`
    from cadwyn_core.schema_generation._annotation_transformer import *  # noqa: F403

sys.modules[__name__] = _annotation_transformer
