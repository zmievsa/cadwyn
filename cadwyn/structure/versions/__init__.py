from cadwyn._utils import present_private_submodules_as_package
from cadwyn.structure.common import VersionType as VersionType

from . import (
    _aliases as _aliases,
)
from . import (
    _background as _background,
)
from . import (
    _body_inputs as _body_inputs,
)
from . import (
    _bundle as _bundle,
)
from . import (
    _changes,
    _requests,
    _responses,
)
from . import (
    _common as _common,
)
from . import (
    _compat as _compat,
)
from . import (
    _core_schema as _core_schema,
)
from . import (
    _dependencies as _dependencies,
)
from . import (
    _enums as _enums,
)
from . import (
    _forms as _forms,
)
from . import (
    _json as _json,
)
from . import (
    _migration_targets as _migration_targets,
)
from . import (
    _request_body as _request_body,
)
from . import (
    _uploads as _uploads,
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
_requests.VersionBundle = VersionBundle
_responses.VersionBundle = VersionBundle

# This package used to be a single module. Keep its objects' __module__ and namespace as they were then.
present_private_submodules_as_package(__name__)
