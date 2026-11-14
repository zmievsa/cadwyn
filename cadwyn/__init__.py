import importlib.metadata

from .applications import Cadwyn
from .dependencies import current_dependency_solver
from .route_generation import VersionedAPIRouter, generate_versioned_routers
from .schema_generation import (
    generate_versioned_models,
    migrate_request,
    migrate_response,
    migrate_response_body,
)
from .structure import (
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

# isort: split
# The FastAPI integration gives the versioning modules what they need from FastAPI. They only need it once Cadwyn is
# used, so it comes after the other imports, which keeps the order in which they import FastAPI.
from ._internal import fastapi_bindings as fastapi_bindings

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "Cadwyn",
    "HeadVersion",
    "MigratedRequest",
    "MigratedResponse",
    "RequestInfo",
    "ResponseInfo",
    "Version",
    "VersionBundle",
    "VersionChange",
    "VersionChangeWithSideEffects",
    "VersionedAPIRouter",
    "convert_request_to_next_version_for",
    "convert_response_to_previous_version_for",
    "current_dependency_solver",
    "endpoint",
    "enum",
    "generate_versioned_models",
    "generate_versioned_routers",
    "migrate_request",
    "migrate_response",
    "migrate_response_body",
    "schema",
]
