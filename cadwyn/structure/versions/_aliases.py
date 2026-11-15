"""`cadwyn_core.structure.versions._aliases` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _aliases

sys.modules[__name__] = _aliases
