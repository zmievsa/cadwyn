import importlib.metadata

from .applications import Cadwyn
from .changelogs import hidden
from .route_generation import VersionedAPIRouter, generate_versioned_routers
from .schema_generation import generate_versioned_models, migrate_response_body
from .structure import (
    HeadVersion,
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

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "Cadwyn",
    "VersionedAPIRouter",
    "VersionBundle",
    "HeadVersion",
    "Version",
    "migrate_response_body",
    "generate_versioned_routers",
    "VersionChange",
    "VersionChangeWithSideEffects",
    "endpoint",
    "schema",
    "enum",
    "convert_response_to_previous_version_for",
    "convert_request_to_next_version_for",
    "RequestInfo",
    "ResponseInfo",
    "generate_versioned_models",
    "hidden",
]
