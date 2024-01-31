import ast
import copy
from typing import Any

from cadwyn._package_utils import IdentifierPythonPath, get_absolute_python_path_of_import
from cadwyn.codegen._asts import (
    get_fancy_repr,
    pop_docstring_from_cls_body,
)
from cadwyn.codegen._common import CodegenContext, PydanticModelWrapper, _EnumWrapper


class ClassRebuildingPlugin:
    node_type = ast.Module

    @staticmethod
    def __call__(node: ast.Module, context: CodegenContext) -> Any:
        if context.current_version_is_latest:
            return node

        node.body = [_migrate_ast_node_to_another_version(n, context) for n in node.body]

        return node


def _migrate_ast_node_to_another_version(
    node: ast.stmt,
    context: CodegenContext,
):
    if isinstance(node, ast.ClassDef):
        return _migrate_cls_to_another_version(node, context)
    elif isinstance(node, ast.ImportFrom):
        python_path = get_absolute_python_path_of_import(node, context.module_python_path)
        node.names = [
            name
            if (name_path := f"{python_path}.{name.name}") not in context.schemas
            else ast.alias(name=context.schemas[name_path].name, asname=name.asname)
            for name in node.names
        ]

    return node


def _migrate_cls_to_another_version(
    cls_node: ast.ClassDef,
    context: CodegenContext,
) -> ast.ClassDef:
    cls_python_path = f"{context.module_python_path}.{cls_node.name}"

    if cls_python_path in context.schemas:
        cls_node = _modify_schema_cls(cls_node, context.schemas, cls_python_path)
    elif cls_python_path in context.enums:
        cls_node = _modify_enum_cls(cls_node, context.enums[cls_python_path])

    if not cls_node.body:
        cls_node.body = [ast.Pass()]
    return cls_node


def _modify_enum_cls(cls_node: ast.ClassDef, enum: _EnumWrapper) -> ast.ClassDef:
    new_body = [
        ast.Assign(
            targets=[ast.Name(member, ctx=ast.Store())],
            value=ast.Name(get_fancy_repr(member_value)),
            lineno=0,
        )
        for member, member_value in enum.members.items()
    ]

    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)]
    docstring = pop_docstring_from_cls_body(old_body)

    cls_node.body = docstring + new_body + old_body
    return cls_node


def _modify_schema_cls(
    cls_node: ast.ClassDef,
    modified_schemas: dict[IdentifierPythonPath, PydanticModelWrapper],
    cls_python_path: str,
) -> ast.ClassDef:
    model_info = modified_schemas[cls_python_path]
    # This is for possible schema renaming
    cls_node.name = model_info.name

    field_definitions = [
        ast.AnnAssign(
            target=ast.Name(name, ctx=ast.Store()),
            annotation=copy.deepcopy(field.annotation_ast),
            # We do this because next plugins **might** use a transformer which will edit the ast within the field
            # and break rendering
            value=copy.deepcopy(field.value_ast),
            simple=1,
        )
        for name, field in model_info.fields.items()
    ]
    validator_definitions = [
        validator.func_ast for validator in model_info.validators.values() if not validator.is_deleted
    ]

    old_body = [
        n
        for n in cls_node.body
        if not (
            isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)
            or (isinstance(n, ast.FunctionDef) and n.name in model_info.validators)
        )
    ]
    docstring = pop_docstring_from_cls_body(old_body)
    cls_node.body = docstring + field_definitions + validator_definitions + old_body
    if not cls_node.body:
        cls_node.body = [ast.Pass()]
    return cls_node
