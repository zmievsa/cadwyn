"""Cadwyn's versioning without FastAPI: versions, version changes, data migrations, and versioned models.

Everything importable from here and from `cadwyn_core.exceptions` is public. `cadwyn` exports the same objects.
"""

import importlib.metadata

__version__ = importlib.metadata.version("cadwyn-core")


def _check_that_cadwyn_has_the_same_version() -> None:
    """Fail at import if `cadwyn` is installed in another version, which is built for its own version of the core"""
    # None in standalone installs, which don't have cadwyn
    cadwyn = next(iter(importlib.metadata.distributions(name="cadwyn")), None)
    cadwyn_version = None if cadwyn is None else cadwyn.version
    if cadwyn_version is not None and cadwyn_version != __version__:
        raise ImportError(
            f"cadwyn-core {__version__} only works with cadwyn {__version__}, but cadwyn {cadwyn_version} is "
            f"installed. Install them in the same version, e.g. with `pip install cadwyn=={__version__}`, or "
            "uninstall cadwyn to use cadwyn-core on its own."
        )


_check_that_cadwyn_has_the_same_version()


# isort: split
# After the version check, so that a mismatch fails with its message rather than with an error of the other code
from .schema_generation import (  # noqa: E402
    SchemaGenerator,
    generate_versioned_models,
    migrate_request,
    migrate_response,
)
from .structure import (  # noqa: E402
    HeadVersion,
    MigratedRequest,
    MigratedResponse,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    VersionChangeWithSideEffects,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    endpoint,
    enum,
    schema,
)

__all__ = [
    "HeadVersion",
    "MigratedRequest",
    "MigratedResponse",
    "RequestInfo",
    "ResponseInfo",
    "SchemaGenerator",
    "Version",
    "VersionBundle",
    "VersionChange",
    "VersionChangeWithSideEffects",
    "convert_request_to_next_version_for",
    "convert_response_to_previous_version_for",
    "endpoint",
    "enum",
    "generate_versioned_models",
    "migrate_request",
    "migrate_response",
    "schema",
]
