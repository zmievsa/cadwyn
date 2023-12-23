import ast
import copy
from typing import Any

from cadwyn._compat import (
    PydanticFieldWrapper,
    get_attrs_that_are_not_from_field_and_that_are_from_field,
    is_pydantic_constrained_type,
)
from cadwyn._package_utils import IdentifierPythonPath, get_absolute_python_path_of_import
from cadwyn.codegen._asts import (
    get_ast_keyword_from_argument_name_and_value,
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
            annotation=_render_annotation(field.get_annotation_for_rendering()),
            value=_generate_field_ast(field),
            simple=1,
        )
        for name, field in model_info.fields.items()
    ]

    old_body = [n for n in cls_node.body if not isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)]
    docstring = pop_docstring_from_cls_body(old_body)
    cls_node.body = docstring + field_definitions + old_body
    if not cls_node.body:
        cls_node.body = [ast.Pass()]
    return cls_node


def _render_annotation(annotation: Any):
    if isinstance(annotation, ast.AST):
        return copy.deepcopy(annotation)
    return ast.parse(get_fancy_repr(annotation), mode="eval").body


def _generate_field_ast(field: PydanticFieldWrapper):
    if field.field_ast is not None:
        # We do this because next plugins **might** use a transformer which will edit the ast within the field
        # and break rendering
        return copy.deepcopy(field.field_ast)
    passed_attrs = field.passed_field_attributes
    if is_pydantic_constrained_type(field.annotation) and field.annotation_ast is None:
        (
            attrs_that_are_only_in_contype,
            attrs_that_are_only_in_field,
        ) = get_attrs_that_are_not_from_field_and_that_are_from_field(field.annotation)
        if not attrs_that_are_only_in_contype:
            passed_attrs |= attrs_that_are_only_in_field

    if passed_attrs:
        return ast.Call(
            func=ast.Name("Field"),
            args=[],
            keywords=[
                get_ast_keyword_from_argument_name_and_value(attr, attr_value)
                for attr, attr_value in passed_attrs.items()
            ],
        )
    return None
