"""`cadwyn_core.structure.versions` under its old name, and the submodules of routes that need FastAPI.

It has every name of `cadwyn_core.structure.versions`, as the same objects. Its other submodules migrate requests and
responses in routes, and the FastAPI integration (`cadwyn._internal.fastapi_bindings`) imports them.
"""

import cadwyn_core.structure.versions
from cadwyn._internal.old_names import keep_names_of_moved_package
from cadwyn_core.structure.versions import APIVersionVarType as APIVersionVarType
from cadwyn_core.structure.versions import HeadVersion as HeadVersion
from cadwyn_core.structure.versions import IdentifierPythonPath as IdentifierPythonPath
from cadwyn_core.structure.versions import PossibleInstructions as PossibleInstructions
from cadwyn_core.structure.versions import Version as Version
from cadwyn_core.structure.versions import VersionBundle as VersionBundle
from cadwyn_core.structure.versions import VersionChange as VersionChange
from cadwyn_core.structure.versions import VersionChangeWithSideEffects as VersionChangeWithSideEffects
from cadwyn_core.structure.versions import VersionType as VersionType
from cadwyn_core.structure.versions import get_cls_pythonpath as get_cls_pythonpath

from . import _aliases as _aliases
from . import _bundle as _bundle
from . import _changes as _changes
from . import _common as _common
from . import _core_schema as _core_schema
from . import _enums as _enums
from . import _json as _json
from . import _migration_targets as _migration_targets
from . import _migrations as _migrations

keep_names_of_moved_package(__name__, cadwyn_core.structure.versions)
