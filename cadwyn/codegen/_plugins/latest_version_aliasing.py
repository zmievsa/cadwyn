import ast
from collections.abc import Collection
from types import ModuleType
from typing import final

import ast_comments

from cadwyn.codegen._common import CodegenContext


class LatestVersionAliasingPlugin:
    node_type = ast.Module

    @staticmethod
    def __call__(
        node: ast.Module,
        context: CodegenContext,
    ):
        if not context.current_version_is_latest:
            return node
        imports = _prepare_imports_from_version_dirs(
            context.template_module,
            ["latest"],
            context.index_of_latest_package_dir_in_module_python_path,
        )

        import_text = ast.unparse(imports[0].get_ast()) + " # noqa: F403"
        if context.extra["ignore_coverage_for_latest_aliases"]:
            import_text += " # pragma: no cover"
        return ast_comments.parse(import_text)


@final
class _ImportedModule:
    __slots__ = (
        "path",
        "name",
        "alias",
        "absolute_python_path_to_origin",
        "how_far_up_is_base_schema_dir_from_current_module",
        "is_package",
    )

    def __init__(
        self,
        version_dir: str,
        import_pythonpath_template: str,
        package_name: str,
        how_far_up_is_base_schema_dir_from_current_module: int,
        absolute_python_path_template: str,
        is_package: bool,
    ) -> None:
        self.path = import_pythonpath_template.format(version_dir)
        self.name = package_name.format(version_dir)
        if self.path == "":
            self.alias = self.name
        else:
            self.alias = f"{self.path.replace('.', '_')}_{self.name}"
        self.absolute_python_path_to_origin = absolute_python_path_template.format("latest")
        self.how_far_up_is_base_schema_dir_from_current_module = how_far_up_is_base_schema_dir_from_current_module
        self.is_package = is_package

    def get_ast(self) -> ast.ImportFrom:
        module = f"{self.path}.{self.name}"
        name = ast.alias(name="*")
        level = self.how_far_up_is_base_schema_dir_from_current_module

        return ast.ImportFrom(
            level=level,
            module=module,
            names=[name],
        )


def _prepare_imports_from_version_dirs(
    original_module: ModuleType,
    version_dir_names: Collection[str],
    index_of_latest_package_dir_in_pythonpath: int,
) -> list[_ImportedModule]:
    # package.latest                     -> from .. import latest
    # package.latest.module              -> from ...latest import module
    # package.latest.subpackage          -> from ...latest import subpackage
    # package.latest.subpackage.module   -> from ....subpackage import module

    # package.latest                    -> from ..latest import *
    # package.latest.module             -> from ..latest.module import *
    # package.latest.subpackage         -> from ...latest.subpackage import *
    # package.latest.subpackage.module  -> from ...latest.subpackage.module import *

    original_module_parts = original_module.__name__.split(".")
    original_module_parts[index_of_latest_package_dir_in_pythonpath] = "{}"
    how_far_up_is_base_schema_dir_from_current_module = (
        len(original_module_parts) - index_of_latest_package_dir_in_pythonpath
    )

    is_package = original_module_parts[-1] == "__init__"
    if is_package:
        original_module_parts.pop(-1)

    package_name = original_module_parts[-1]
    package_path = original_module_parts[index_of_latest_package_dir_in_pythonpath:-1]
    import_pythonpath_template = ".".join(package_path)
    absolute_python_path_template = ".".join(original_module_parts)
    return [
        _ImportedModule(
            version_dir,
            import_pythonpath_template,
            package_name,
            how_far_up_is_base_schema_dir_from_current_module,
            absolute_python_path_template,
            is_package,
        )
        for version_dir in version_dir_names
    ]
