import ast
import builtins
import dis
import inspect
import sys
import textwrap
from enum import Enum
from types import CodeType, FunctionType
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

import typer
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from cadwyn._asts import _ReprContext, get_fancy_repr, pop_docstring_from_cls_body
from cadwyn._utils import is_same_value, lenient_issubclass
from cadwyn.exceptions import CadwynRenderError
from cadwyn.schema_generation import (
    SchemaGenerator,
    _EnumWrapper,
    _PydanticModelWrapper,
    generate_versioned_models,
)
from cadwyn.structure.versions import VersionBundle, get_cls_pythonpath

from ._importer import import_attribute_from_string, import_module_from_string

if TYPE_CHECKING:
    from cadwyn.applications import Cadwyn


def render_module_by_path(module_path: str, app_path: str, version: str):
    module = import_module_from_string(module_path)
    app: Cadwyn = import_attribute_from_string(app_path)
    attributes_to_alter = [
        name
        for name, value in module.__dict__.items()
        if lenient_issubclass(value, (Enum, BaseModel)) and value.__module__ == module.__name__
    ]

    try:
        module_ast = ast.parse(inspect.getsource(module))
    except (OSError, SyntaxError, ValueError) as e:  # pragma: no cover
        raise CadwynRenderError(f"Failed to find the source for module {module.__name__}") from e

    return ast.unparse(
        ast.Module(
            body=[
                _render_model_from_ast(node, getattr(module, node.name), app.versions, version)
                if isinstance(node, ast.ClassDef) and node.name in attributes_to_alter
                else node
                for node in module_ast.body
            ],
            type_ignores=module_ast.type_ignores,
        )
    )


def render_model_by_path(model_path: str, app_path: str, version: str) -> str:
    # cadwyn render model schemas:MySchema --app=run:app --version=2000-01-01
    model: type[Union[BaseModel, Enum]] = import_attribute_from_string(model_path)
    app: Cadwyn = import_attribute_from_string(app_path)
    return render_model(model, app.versions, version)


def render_model(model: type[Union[BaseModel, Enum]], versions: VersionBundle, version: str) -> str:
    try:
        original_cls_node = ast.parse(textwrap.dedent(inspect.getsource(model))).body[0]
    except (OSError, SyntaxError, ValueError):  # pragma: no cover
        typer.echo(f"Failed to find the source for model {get_cls_pythonpath(model)}")
        return f"class {model.__name__}: 'failed to find the original class source'"
    if not isinstance(original_cls_node, ast.ClassDef):
        raise TypeError(f"{get_cls_pythonpath(model)} is not a class")

    return ast.unparse(_render_model_from_ast(original_cls_node, model, versions, version))


def _render_model_from_ast(
    model_ast: ast.ClassDef, model: type[Union[BaseModel, Enum]], versions: VersionBundle, version: str
):
    versioned_models = generate_versioned_models(versions)
    generator = versioned_models[version]
    wrapper = generator._get_wrapper_for_model(model)

    if isinstance(wrapper, _EnumWrapper):
        return _render_enum_model(wrapper, model_ast)
    else:
        return _render_pydantic_model(wrapper, model_ast, generator)


def _render_enum_model(wrapper: _EnumWrapper[Enum], original_cls_node: ast.ClassDef):
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
    return original_cls_node


def _render_pydantic_model(
    wrapper: _PydanticModelWrapper[BaseModel], original_cls_node: ast.ClassDef, generator: SchemaGenerator
):
    # This is for possible schema renaming
    original_cls_node.name = wrapper.name

    # The same fields as in the copies of the model
    field_attributes, annotations = wrapper.get_field_attributes_and_annotations(generator.model_bundle.schemas)
    # Without config instructions `cadwyn render` omits model_config, so we render the aliases that an alias
    # generator gives the fields whose alias an instruction changed. We only generate the model for such fields
    if wrapper.cls.model_config.get("alias_generator") is not None:
        defined_fields = wrapper.fields_renamed_by_parents | wrapper.fields
        if names := [name for name in field_attributes if defined_fields[name].keys_renamed_by_alias]:
            generated_fields = generator[wrapper.cls].model_fields
            for name in names:
                field_attributes[name] = _with_aliases_of_generated_field(
                    field_attributes[name], generated_fields[name]
                )
    field_definitions = []
    for name, attributes in field_attributes.items():
        try:
            field_definitions.append(
                ast.AnnAssign(
                    target=ast.Name(name),
                    annotation=ast.Name(get_fancy_repr(annotations[name])),
                    value=_generate_field_ast(
                        _without_redundant_none_aliases(attributes, annotations[name]),
                        vars(sys.modules[wrapper.cls.__module__]),
                    ),
                    simple=1,
                )
            )
        except CadwynRenderError as error:
            raise CadwynRenderError(f"Cannot render field {wrapper.name}.{name}: {error}") from error
    validator_definitions = [
        ast.parse(textwrap.dedent(inspect.getsource(validator.func))).body[0]
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
    # We keep the docstring from the source unless an instruction changed it
    if wrapper.doc != wrapper.cls.__doc__:
        docstring = [ast.Expr(ast.Constant(wrapper.doc))] if wrapper.doc else []
    config_definitions = []
    config = wrapper.other_attributes["model_config"]
    if config_changes := wrapper.get_config_changes(generator.model_bundle.schemas):
        config = config | config_changes
        config_values = []
        for key, value in config.items():
            try:
                namespace = vars(sys.modules[wrapper.cls.__module__])
                if callable(value):
                    _validate_config_callable(value, namespace, wrapper.name)
                expression = ast.parse(_field_attribute_repr(value, namespace, lambda_keyword=key), mode="eval").body
                if isinstance(expression, ast.Lambda):
                    _validate_config_lambda_defaults(value, expression)
                config_values.append(expression)
            except Exception as error:  # User callables and reprs can fail in arbitrary ways.
                raise CadwynRenderError(f"Cannot render config {wrapper.name}.{key}: {error}") from error
        config_definitions = [
            ast.Assign(
                targets=[ast.Name("model_config", ctx=ast.Store())],
                value=ast.Dict(keys=[ast.Constant(key) for key in config], values=config_values),
                lineno=0,
            )
        ]
    body = config_definitions + field_definitions + validator_definitions + old_body
    if not docstring:
        # A string at the start of the body would become the docstring, e.g. the documentation of a ClassVar
        # whose definition we do not render, so we skip such strings
        while pop_docstring_from_cls_body(body):
            pass
    original_cls_node.body = docstring + body
    if not original_cls_node.body:
        original_cls_node.body = [ast.Pass()]
    return original_cls_node


_ALIAS_ATTRIBUTES = ("alias", "validation_alias", "serialization_alias")

_CONFIG_LITERAL_TYPES = (type(None), bool, int, float, complex, str, bytes)


def _validate_config_callable(value: object, namespace: dict[str, Any], model_name: str) -> None:
    if not isinstance(value, FunctionType):
        raise CadwynRenderError("Only Python functions can be rendered as config callables.")
    defaults = (*(value.__defaults__ or ()), *(value.__kwdefaults__ or {}).values())
    if any(type(default) not in _CONFIG_LITERAL_TYPES for default in defaults):
        raise CadwynRenderError("Config callable defaults must be plain literals.")
    module_builtins = namespace.get("__builtins__", builtins)
    rendered_globals = getattr(module_builtins, "__dict__", module_builtins) | namespace
    # Executing the rendered class replaces this binding, so it cannot remain the original object.
    rendered_globals.pop(model_name, None)
    if value.__name__ != "<lambda>" and rendered_globals.get(value.__name__) is not value:
        raise CadwynRenderError("Cannot resolve the config function to the same object in the rendered module.")
    for name, cell in zip(value.__code__.co_freevars, value.__closure__ or (), strict=True):
        if name not in rendered_globals or rendered_globals[name] is not cell.cell_contents:
            raise CadwynRenderError("Config callable has captured variables unavailable in the rendered module.")
    original_globals = value.__builtins__ | value.__globals__
    codes = [value.__code__]
    while codes:
        code = codes.pop()
        codes.extend(constant for constant in code.co_consts if isinstance(constant, CodeType))
        for instruction in dis.get_instructions(code):
            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}:
                name = instruction.argval
                if (
                    name not in original_globals
                    or name not in rendered_globals
                    or original_globals[name] is not rendered_globals[name]
                ):
                    raise CadwynRenderError(
                        f"Cannot resolve config global {name!r} to the same object in the rendered module."
                    )


def _validate_config_lambda_defaults(value: FunctionType, expression: ast.Lambda) -> None:
    nodes = [*expression.args.defaults, *(node for node in expression.args.kw_defaults if node is not None)]
    defaults = (
        *(value.__defaults__ or ()),
        *(
            (value.__kwdefaults__ or {})[arg.arg]
            for arg, node in zip(expression.args.kwonlyargs, expression.args.kw_defaults, strict=True)
            if node is not None
        ),
    )
    for node, default in zip(nodes, defaults, strict=True):
        try:
            literal = ast.literal_eval(node)
        except (ValueError, TypeError) as error:
            raise CadwynRenderError("Config lambda defaults must be plain literals in the source.") from error
        if type(literal) not in _CONFIG_LITERAL_TYPES or not is_same_value(literal, default):
            raise CadwynRenderError("Config lambda defaults differ from their source literals.")


def _with_aliases_of_generated_field(attributes: dict[str, Any], generated_field: FieldInfo) -> dict[str, Any]:
    """Pass the aliases that the generated field has explicitly where Field(**attributes) wouldn't get them"""
    attributes = attributes.copy()
    for attr_name in _ALIAS_ATTRIBUTES:
        generated_value = getattr(generated_field, attr_name)
        if generated_value is not None and _get_alias_of_field(attributes, attr_name) != generated_value:
            attributes[attr_name] = generated_value
    return attributes


def _get_alias_of_field(attributes: dict[str, Any], attr_name: str) -> Any:
    """Return the alias that Field(**attributes) gets. Like in Field(), the missing aliases follow alias"""
    if attributes.get(attr_name) is None and attr_name != "alias":
        return attributes.get("alias")
    return attributes.get(attr_name)


def _without_redundant_none_aliases(attributes: dict[str, Any], annotation: Any) -> dict[str, Any]:
    """Skip the aliases that are None if Field() is the same without them.

    Cadwyn passes the deleted aliases as None so that they override the ones from the Field() in the annotation of
    the field. Without such a Field() and without an alias for the others to follow, None is the same as no alias.
    """
    if attributes.get("alias") is not None or (
        get_origin(annotation) is Annotated
        and any(isinstance(metadata, FieldInfo) for metadata in get_args(annotation)[1:])
    ):
        return attributes
    return {
        attr_name: attr_value
        for attr_name, attr_value in attributes.items()
        if not (attr_name in _ALIAS_ATTRIBUTES and attr_value is None)
    }


def _generate_field_ast(field_attributes: dict[str, Any], namespace: dict[str, Any]) -> ast.Call:
    return ast.Call(
        func=ast.Name("Field"),
        args=[],
        keywords=[
            ast.keyword(
                arg=attr,
                value=ast.parse(_field_attribute_repr(attr_value, namespace), mode="eval").body,
            )
            for attr, attr_value in field_attributes.items()
        ],
    )


def _field_attribute_repr(value: Any, namespace: dict[str, Any], *, lambda_keyword: str = "default_factory") -> str:
    context = _ReprContext(lambda_keyword=lambda_keyword)
    context.namespace.update(namespace)
    return "type(None)" if value is type(None) else get_fancy_repr(value, context)
