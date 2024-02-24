import importlib.metadata

from .applications import Cadwyn
from .codegen import generate_code_for_versioned_packages
from .routing import InternalRepresentationOf, VersionedAPIRouter, generate_versioned_routers
from .structure import VersionBundle

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "Cadwyn",
    "VersionedAPIRouter",
    "generate_code_for_versioned_packages",
    "VersionBundle",
    "generate_versioned_routers",
    "InternalRepresentationOf",
]
