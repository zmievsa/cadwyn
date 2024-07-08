import importlib.metadata

from .applications import Cadwyn
from .route_generation import VersionedAPIRouter, generate_versioned_routers
from .structure import HeadVersion, Version, VersionBundle

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "Cadwyn",
    "VersionedAPIRouter",
    "VersionBundle",
    "HeadVersion",
    "Version",
    "generate_versioned_routers",
]
