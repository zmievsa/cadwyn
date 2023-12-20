import ast
import inspect
from datetime import date
from pathlib import Path
from types import ModuleType

from cadwyn.exceptions import CodeGenerationError


def get_absolute_python_path_of_import(node: ast.ImportFrom, module_python_path: str):
    python_path = ".".join(module_python_path.split(".")[0 : -node.level])
    result = []
    if node.module:
        result.append(node.module)
    if python_path:
        result.append(python_path)
    return ".".join(result)


def get_pythonpath(cls: type):
    return f"{cls.__module__}.{cls.__name__}"


def get_version_dir_path(template_module: ModuleType, version: date) -> Path:
    template_dir = get_package_path_from_module(template_module)
    return template_dir.with_name(get_version_dir_name(version))


def get_package_path_from_module(template_module: ModuleType) -> Path:
    file = inspect.getsourcefile(template_module)

    # I am too lazy to reproduce this error correctly
    if file is None:  # pragma: no cover
        raise CodeGenerationError(f'Module "{template_module}" has no source file')
    file = Path(file)
    if not file.name == "__init__.py":
        raise CodeGenerationError(f'Module "{template_module}" is not a package')
    return file.parent


def get_version_dir_name(version: date):
    return "v" + version.isoformat().replace("-", "_")
