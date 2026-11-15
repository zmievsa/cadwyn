"""`cadwyn_core._asts` under its old name, as the same module object, so imports and patches of either name work."""

import sys

from cadwyn_core import _asts

sys.modules[__name__] = _asts
