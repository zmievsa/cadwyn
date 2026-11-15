"""`cadwyn_core.structure.versions._changes` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _changes

sys.modules[__name__] = _changes
