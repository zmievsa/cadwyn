import importlib
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from universi.exceptions import UniversiError

Sentinel: Any = object()


def get_another_version_of_cls(
    cls_from_old_version: type[Any],
    new_version_dir: Path,
) -> None:
    # version_dir = /home/myuser/package/companies/v2021_01_01

    module_from_old_version = sys.modules[cls_from_old_version.__module__]
    module = get_another_version_of_module(module_from_old_version, new_version_dir)
    return getattr(module, cls_from_old_version.__name__)


def get_another_version_of_module(
    module_from_old_version: ModuleType,
    new_version_dir: Path,
):
    new_model_module_python_path = get_pythonpath_to_another_version_of_module(
        module_from_old_version,
        new_version_dir,
    )
    module = importlib.import_module(new_model_module_python_path)
    return module


def get_pythonpath_to_another_version_of_module(
    module_from_old_version: ModuleType,
    new_version_dir: Path,
) -> str:
    # ['package', 'companies', 'latest', 'schemas']
    #                           ^^^^^^
    #                           index = 2
    index_of_base_schema_dir = get_index_of_base_schema_dir_in_pythonpath(
        module_from_old_version,
        new_version_dir,
    )

    # ['package', 'companies', 'latest', 'schemas']
    model_split_python_path = module_from_old_version.__name__.split(".")
    # ['package', 'companies', 'v2021_01_01', 'schemas']
    model_split_python_path[index_of_base_schema_dir] = new_version_dir.name
    # package.companies.v2021_01_01.schemas
    new_model_module_python_path = ".".join(model_split_python_path)
    return new_model_module_python_path


def get_index_of_base_schema_dir_in_pythonpath(
    module_from_old_version: ModuleType,
    parallel_dir: Path,
) -> int:
    file = inspect.getsourcefile(module_from_old_version)
    if file is None:
        # Seems quite unnecessary to cover
        raise UniversiError(
            f"Model {module_from_old_version} is not defined in a file",
        )  # pragma: no cover
    # /home/myuser/package/companies/latest/__init__.py
    file = Path(file)
    if file.name == "__init__.py":
        # /home/myuser/package/companies/latest/
        file = file.parent
    # /home/myuser/package/companies
    root_dir = parallel_dir.parent
    # latest/schemas
    relative_file = file.relative_to(root_dir).with_suffix("")
    # ['latest', 'schemas']
    relative_file_parts = relative_file.parts
    # package.companies.latest.schemas.Payable
    module_python_path = module_from_old_version.__name__
    # ['package', 'companies', 'latest', 'schemas']
    module_split_python_path = module_python_path.split(".")

    return len(module_split_python_path) - len(relative_file_parts)
