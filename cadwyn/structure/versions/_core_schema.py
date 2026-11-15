"""`cadwyn_core.structure.versions._core_schema` under its old name, as the same module object."""

import sys

from cadwyn_core.structure.versions import _core_schema

sys.modules[__name__] = _core_schema
