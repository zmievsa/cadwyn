import functools
import importlib
import inspect
from collections.abc import Callable, Collection
from pathlib import Path
from types import ModuleType
from typing import Any, Generic, TypeVar, Union

from cadwyn.exceptions import CadwynError, ModuleIsNotVersionedError

Sentinel: Any = object()
UnionType = type(int | str) | type(Union[int, str])
_T = TypeVar("_T", bound=Callable)


_P_T = TypeVar("_P_T")
_P_R = TypeVar("_P_R")


class classproperty(Generic[_P_T, _P_R]):  # noqa: N801
    def __init__(self, func: Callable[[_P_T], _P_R]) -> None:
        super().__init__()
        self.func = func

    def __get__(self, obj: Any, cls: _P_T) -> _P_R:
        return self.func(cls)


class PlainRepr(str):
    """String class where repr doesn't include quotes"""

    def __repr__(self) -> str:
        return str(self)


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
    index_of_base_schema_dir = get_index_of_latest_schema_dir_in_module_python_path(
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
def get_index_of_latest_schema_dir_in_module_python_path(
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
    is_package = file.name == "__init__.py"
    if is_package:
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

    index = len(module_split_python_path) - len(relative_file_parts) - int(is_package)

    # When we are in latest/__init__.py, we have this special case
    if len(relative_file_parts) == 1 and is_package:
        index += 1
    return index


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
