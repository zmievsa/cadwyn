from cadwyn._utils import present_private_submodules_as_package
from cadwyn.structure.common import VersionType as VersionType

# Only the submodules that work without FastAPI. The FastAPI integration (`cadwyn._internal.fastapi_bindings`) imports
# the ones that migrate requests and responses in routes.
from . import (
    _aliases as _aliases,
)
from . import (
    _bundle as _bundle,
)
from . import (
    _changes,
)
from . import (
    _common as _common,
)
from . import (
    _core_schema as _core_schema,
)
from . import (
    _enums as _enums,
)
from . import (
    _json as _json,
)
from . import (
    _migration_targets as _migration_targets,
)
from . import (
    _migrations as _migrations,
)
from ._bundle import VersionBundle as VersionBundle
from ._changes import APIVersionVarType as APIVersionVarType
from ._changes import HeadVersion as HeadVersion
from ._changes import IdentifierPythonPath as IdentifierPythonPath
from ._changes import PossibleInstructions as PossibleInstructions
from ._changes import Version as Version
from ._changes import VersionChange as VersionChange
from ._changes import VersionChangeWithSideEffects as VersionChangeWithSideEffects
from ._changes import get_cls_pythonpath as get_cls_pythonpath

# These modules refer back to the bundle in annotations. Bind it after its definition is complete.
_changes.VersionBundle = VersionBundle
_migrations.VersionBundle = VersionBundle

# This package used to be a single module. Keep its objects' __module__ and namespace as they were then.
present_private_submodules_as_package(__name__)
