import ast
from typing import Any

from cadwyn._codegen.common import CodegenContext, PydanticModelWrapper
from cadwyn._package_utils import IdentifierPythonPath


class ClassRenamingPlugin:
    node_type = ast.Module

    @staticmethod
    def __call__(
        node: ast.Module,
        context: CodegenContext,
    ):
        if context.current_version_is_latest:
            return node
        return _AnnotationASTNodeTransformerWithSchemaRenaming(
            context.schemas,
            context.all_names_defined_on_toplevel_of_file,
            context.module_python_path,
        ).visit(node)


class _AnnotationASTNodeTransformerWithSchemaRenaming(ast.NodeTransformer):
    def __init__(
        self,
        modified_schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
        all_names_in_module: dict[str, str],
        module_python_path: str,
    ):
        super().__init__()
        self.modified_schemas = modified_schemas
        self.module_python_path = module_python_path
        self.all_names_in_module = all_names_in_module

    def visit_Name(self, node: ast.Name) -> Any:  # noqa: N802
        return self._get_name(node, node.id)

    def _get_name(self, node: ast.AST, name: str):
        model_info = self.modified_schemas.get(f"{self.all_names_in_module.get(name, self.module_python_path)}.{name}")
        if model_info is not None:
            return ast.Name(model_info.name)
        return node
