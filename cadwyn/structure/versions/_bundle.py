"""`cadwyn_core.structure.versions._bundle` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _bundle

sys.modules[__name__] = _bundle
