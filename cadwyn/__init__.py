import importlib.metadata

from .codegen import regenerate_dir_to_all_versions
from .header import get_cadwyn_dependency
from .routing import VersionedAPIRouter, generate_all_router_versions
from .structure import VersionBundle

__version__ = importlib.metadata.version("cadwyn")
__all__ = [
    "VersionedAPIRouter",
    "get_cadwyn_dependency",
    "regenerate_dir_to_all_versions",
    "VersionBundle",
    "generate_all_router_versions",
]
