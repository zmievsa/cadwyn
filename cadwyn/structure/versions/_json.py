"""`cadwyn_core.structure.versions._json` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _json

sys.modules[__name__] = _json
