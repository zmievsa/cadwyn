"""`cadwyn_core.structure.data` under its old name, as the same module object."""

import sys

from cadwyn_core.structure import data

sys.modules[__name__] = data
