import ast
import inspect
from enum import Enum

from pydantic import BaseModel
from uvicorn import __main__

from cadwyn._asts import get_fancy_repr, pop_docstring_from_cls_body
from cadwyn._package_utils import get_cls_pythonpath
from cadwyn.applications import Cadwyn
from cadwyn.runtime_compat import PydanticFieldWrapper, _EnumWrapper, _PydanticRuntimeModelWrapper
from cadwyn.schema_generation import _generate_versioned_models
from cadwyn.structure.versions import VersionBundle

from ._importer import import_from_string


def render_model_by_path(model_path: str, app_path: str, version: str) -> str:
    # cadwyn render model schemas:MySchema --app=run:app --version=2000-01-01
    model: type[BaseModel | Enum] = import_from_string(model_path)
    app: Cadwyn = import_from_string(app_path)
    return render_model(model, app.versions, version)


def render_model(model: type[BaseModel | Enum], versions: VersionBundle, version: str) -> str:
    versioned_models = _generate_versioned_models(versions)
    generator = versioned_models[version]
    wrapper = generator._get_wrapper_for_model(model)

    if isinstance(wrapper, _EnumWrapper):
        return _render_enum_model(wrapper)
    else:
        return _render_pydantic_model(wrapper)


def _render_enum_model(wrapper: _EnumWrapper) -> str:
    try:
        original_cls_node = ast.parse(inspect.getsource(wrapper.cls)).body[0]
    except (OSError, SyntaxError, ValueError):
        print(f"Failed to find the source for model {get_cls_pythonpath(wrapper.cls)}")
        return f"class {wrapper.cls.__name__}: 'failed to find the original class source'"
    if not isinstance(original_cls_node, ast.ClassDef):
        raise ValueError(f"{get_cls_pythonpath(wrapper.cls)} is not a class")
    # This is for possible schema renaming
    original_cls_node.name = wrapper.cls.__name__

    new_body = [
        ast.Assign(
            targets=[ast.Name(member, ctx=ast.Store())],
            value=ast.Name(get_fancy_repr(member_value)),
            lineno=0,
        )
        for member, member_value in wrapper.members.items()
    ]

    old_body = [
        n for n in original_cls_node.body if not isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)
    ]
    docstring = pop_docstring_from_cls_body(old_body)

    original_cls_node.body = docstring + new_body + old_body
    if not original_cls_node.body:
        original_cls_node.body = [ast.Pass()]
    return ast.unparse(original_cls_node)


def _render_pydantic_model(wrapper: _PydanticRuntimeModelWrapper) -> str:
    try:
        original_cls_node = ast.parse(inspect.getsource(wrapper.cls)).body[0]
    except (OSError, SyntaxError, ValueError):
        print(f"Failed to find the source for model {get_cls_pythonpath(wrapper.cls)}")
        return f"class {wrapper.cls.__name__}: 'failed to find the original class source'"
    if not isinstance(original_cls_node, ast.ClassDef):
        raise ValueError(f"{get_cls_pythonpath(wrapper.cls)} is not a class")

    # This is for possible schema renaming
    original_cls_node.name = wrapper.name

    field_definitions = [
        ast.AnnAssign(
            target=ast.Name(name),
            annotation=ast.Name(get_fancy_repr(field.annotation)),
            value=_generate_field_ast(field),
            simple=1,
        )
        for name, field in wrapper.fields.items()
    ]
    validator_definitions = [
        ast.parse(inspect.getsource(validator.func)).body[0]
        for validator in wrapper.validators.values()
        if not validator.is_deleted
    ]

    old_body = [
        n
        for n in original_cls_node.body
        if not (
            isinstance(n, ast.AnnAssign | ast.Assign | ast.Pass | ast.Constant)
            or (isinstance(n, ast.FunctionDef) and n.name in wrapper.validators)
        )
    ]
    docstring = pop_docstring_from_cls_body(old_body)
    original_cls_node.body = docstring + field_definitions + validator_definitions + old_body
    if not original_cls_node.body:
        original_cls_node.body = [ast.Pass()]
    return ast.unparse(original_cls_node)


def _generate_field_ast(field: PydanticFieldWrapper) -> ast.Call:
    return ast.Call(
        func=ast.Name("Field"),
        args=[],
        keywords=[
            ast.keyword(arg=attr, value=ast.parse(get_fancy_repr(attr_value), mode="eval").body)
            for attr, attr_value in field.passed_field_attributes.items()
        ],
    )
