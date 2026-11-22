"""`cadwyn_core.structure.versions._json` under its old name, as the same module object."""

import sys
from typing import TYPE_CHECKING

from cadwyn_core.structure.versions import _json

if TYPE_CHECKING:
    from cadwyn_core.structure.versions._json import *  # noqa: F403

sys.modules[__name__] = _json
