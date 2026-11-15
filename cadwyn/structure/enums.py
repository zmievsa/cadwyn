"""`cadwyn_core.structure.enums` under its old name, as the same module object."""

import sys

from cadwyn_core.structure import enums

sys.modules[__name__] = enums
