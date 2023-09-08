import importlib.metadata

from .codegen import regenerate_dir_to_all_versions
from .header import api_version_var, get_universi_dependency
from .routing import VersionedAPIRouter
from .structure import VersionBundle

__version__ = importlib.metadata.version("universi")
__all__ = [
    "VersionedAPIRouter",
    "get_universi_dependency",
    "api_version_var",
    "regenerate_dir_to_all_versions",
    "VersionBundle",
]
