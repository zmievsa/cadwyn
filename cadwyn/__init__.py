import importlib.metadata

from .codegen import generate_code_for_versioned_packages
from .header import get_cadwyn_dependency
from .routing import VersionedAPIRouter, generate_versioned_routers
from .structure import VersionBundle, internal_body_representation_of

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "VersionedAPIRouter",
    "get_cadwyn_dependency",
    "generate_code_for_versioned_packages",
    "VersionBundle",
    "generate_versioned_routers",
    "internal_body_representation_of",
]
