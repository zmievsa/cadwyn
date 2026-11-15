"""`cadwyn_core.structure` under its old name, with the same objects.

It's a package of its own, not the moved one, because `cadwyn.structure.versions` still has the submodules that
migrate requests and responses in routes.
"""

import cadwyn_core.structure
from cadwyn._internal.old_names import keep_names_of_moved_package
from cadwyn_core.structure import (
    HeadVersion as HeadVersion,
)
from cadwyn_core.structure import (
    MigratedRequest as MigratedRequest,
)
from cadwyn_core.structure import (
    MigratedResponse as MigratedResponse,
)
from cadwyn_core.structure import (
    RequestInfo as RequestInfo,
)
from cadwyn_core.structure import (
    ResponseInfo as ResponseInfo,
)
from cadwyn_core.structure import (
    Version as Version,
)
from cadwyn_core.structure import (
    VersionBundle as VersionBundle,
)
from cadwyn_core.structure import (
    VersionChange as VersionChange,
)
from cadwyn_core.structure import (
    VersionChangeWithSideEffects as VersionChangeWithSideEffects,
)
from cadwyn_core.structure import (
    convert_request_to_next_version_for as convert_request_to_next_version_for,
)
from cadwyn_core.structure import (
    convert_response_to_previous_version_for as convert_response_to_previous_version_for,
)
from cadwyn_core.structure import (
    endpoint as endpoint,
)
from cadwyn_core.structure import (
    enum as enum,
)
from cadwyn_core.structure import (
    schema as schema,
)

from . import common as common
from . import data as data
from . import endpoints as endpoints
from . import enums as enums
from . import schemas as schemas
from . import versions as versions

__all__ = [
    "HeadVersion",
    "MigratedRequest",
    "MigratedResponse",
    "RequestInfo",
    "ResponseInfo",
    "Version",
    "VersionBundle",
    "VersionChange",
    "VersionChangeWithSideEffects",
    "convert_request_to_next_version_for",
    "convert_response_to_previous_version_for",
    "endpoint",
    "enum",
    "schema",
]

keep_names_of_moved_package(__name__, cadwyn_core.structure)
