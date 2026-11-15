"""`cadwyn_core.structure.schemas` under its old name, as the same module object."""

import sys

from cadwyn_core.structure import schemas

sys.modules[__name__] = schemas
