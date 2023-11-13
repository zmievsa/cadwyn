import functools
import importlib
import inspect
from collections.abc import Callable, Collection
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar, Union

from cadwyn.exceptions import CadwynError, ModuleIsNotVersionedError

Sentinel: Any = object()
UnionType = type(int | str) | type(Union[int, str])
_T = TypeVar("_T", bound=Callable)


def same_definition_as_in(t: _T) -> Callable[[Callable], _T]:
    def decorator(f: Callable) -> _T:
        return f  # pyright: ignore[reportGeneralTypeIssues]

    return decorator


def get_another_version_of_module(
    module_from_old_version: ModuleType,
    new_version_dir: Path,
    version_dirs: frozenset[Path],
):
    new_model_module_python_path = get_pythonpath_to_another_version_of_module(
        module_from_old_version,
        new_version_dir,
        version_dirs,
    )
    return importlib.import_module(new_model_module_python_path)


def get_pythonpath_to_another_version_of_module(
    module_from_old_version: ModuleType,
    new_version_dir: Path,
    version_dirs: frozenset[Path],
) -> str:
    # ['package', 'companies', 'latest', 'schemas']
    #                           ^^^^^^
    #                           index = 2
    index_of_base_schema_dir = get_index_of_base_schema_dir_in_pythonpath(
        module_from_old_version,
        new_version_dir,
        version_dirs,
    )

    # ['package', 'companies', 'latest', 'schemas']
    model_split_python_path = module_from_old_version.__name__.split(".")
    # ['package', 'companies', 'v2021_01_01', 'schemas']
    model_split_python_path[index_of_base_schema_dir] = new_version_dir.name
    # package.companies.v2021_01_01.schemas
    return ".".join(model_split_python_path)


@functools.cache
def get_index_of_base_schema_dir_in_pythonpath(
    module_from_old_version: ModuleType,
    parallel_dir: Path,
    version_dirs: frozenset[Path] = frozenset(),
) -> int:
    """If version_dirs have been passed, we will check if the module is versioned and raise an exception if it isn't"""
    file = inspect.getsourcefile(module_from_old_version)
    # Impossible to cover
    if file is None:  # pragma: no cover
        raise CadwynError(
            f"Model {module_from_old_version} is not defined in a file. It is likely because it's a compiled module "
            "which Cadwyn couldn't migrate to an older version. "
            "If you are seeing this error -- you've encountered a bug in Cadwyn.",
        )
    # /home/myuser/package/companies/latest/__init__.py
    file = Path(file)
    _validate_that_module_is_versioned(file, version_dirs)
    if file.name == "__init__.py":
        # /home/myuser/package/companies/latest/
        file = file.parent
    # /home/myuser/package/companies
    root_dir = parallel_dir.parent
    # latest/schemas
    relative_file = file.relative_to(root_dir).with_suffix("")
    # ['latest', 'schemas']
    relative_file_parts = relative_file.parts
    # package.companies.latest.schemas.payables
    module_python_path = module_from_old_version.__name__
    # ['package', 'companies', 'latest', 'schemas']
    module_split_python_path = module_python_path.split(".")

    return len(module_split_python_path) - len(relative_file_parts)


def _validate_that_module_is_versioned(file: Path, version_dirs: Collection[Path]):
    if not version_dirs:
        return
    for version_dir in version_dirs:
        try:
            file.relative_to(version_dir)
            return
        except ValueError:
            pass
    raise ModuleIsNotVersionedError(f"Module {file} is not versioned.")
