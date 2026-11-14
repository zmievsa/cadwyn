"""Cadwyn's versioning without FastAPI: versions, version changes, data migrations, and versioned models.

It's empty for now: the modules of `cadwyn` that work without FastAPI move here next
(https://github.com/zmievsa/cadwyn/issues/203).
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
