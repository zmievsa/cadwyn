from warnings import warn

from .applications import Cadwyn

warn(
    "'cadwyn.main' module is deprecated. Please use 'cadwyn.applications' instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["Cadwyn"]
