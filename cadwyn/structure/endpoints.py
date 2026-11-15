"""`cadwyn_core.structure.endpoints` under its old name, as the same module object."""

import sys

from cadwyn_core.structure import endpoints

sys.modules[__name__] = endpoints
