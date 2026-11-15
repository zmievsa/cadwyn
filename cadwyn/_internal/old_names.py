"""The old names in `cadwyn` of what moved to `cadwyn_core`."""

import sys
from types import ModuleType


def keep_names_of_moved_package(package_name: str, moved_package: ModuleType) -> None:
    """Give a package of `cadwyn` every name of its moved version in `cadwyn_core`, as the same objects.

    Code imports private names from these packages too, which used to have everything that moved. Their own submodules
    are imported under their old names instead, because some of them stayed in `cadwyn`.
    """
    namespace = vars(sys.modules[package_name])
    for name, value in vars(moved_package).items():
        if name.startswith("__") or (
            isinstance(value, ModuleType) and value.__name__.startswith(f"{moved_package.__name__}.")
        ):
            continue
        if namespace.setdefault(name, value) is not value:  # pragma: no cover # Guards future moves
            raise ImportError(f"{package_name}.{name} differs from {moved_package.__name__}.{name}")
