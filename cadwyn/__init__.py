import importlib.metadata

from .codegen import generate_code_for_versioned_packages
from .main import Cadwyn
from .routing import VersionedAPIRouter, generate_versioned_routers
from .structure import VersionBundle, internal_body_representation_of

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "Cadwyn",
    "VersionedAPIRouter",
    "generate_code_for_versioned_packages",
    "VersionBundle",
    "generate_versioned_routers",
    "internal_body_representation_of",
]
