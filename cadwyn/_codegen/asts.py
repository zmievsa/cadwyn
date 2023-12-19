import ast
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from functools import cache
from pathlib import Path
from types import GenericAlias, LambdaType, ModuleType, NoneType
from typing import TYPE_CHECKING, Any, _BaseGenericAlias, cast, get_args, get_origin

from pydantic import BaseModel

from cadwyn._compat import PYDANTIC_V2, ModelField
from cadwyn._package_utils import _get_absolute_python_path_of_import
from cadwyn._utils import UnionType
from cadwyn.exceptions import CodeGenerationError, InvalidGenerationInstructionError

if TYPE_CHECKING:
    import annotated_types

    from cadwyn.codegen import _ModelFieldLike


_LambdaFunctionName = (lambda: None).__name__  # pragma: no branch


def _parse_python_module(module: ModuleType) -> tuple[ast.Module, str]:
    try:
        source = inspect.getsource(module)
        return ast.parse(source), source
    except OSError as e:
        if module.__file__ is None:  # pragma: no cover
            raise CodeGenerationError("Failed to get file path to the module") from e

        path = Path(module.__file__)
        if path.is_file() and path.read_text() == "":
            return ast.Module([]), ""
        # Not sure how to get here so this is just a precaution
        raise CodeGenerationError(
            "Failed to get source code for module. This likely means that there is a bug in Cadwyn. "
            "Please, report this to our issue tracker.",
        ) from e  # pragma: no cover


class PlainRepr(str):
    """String class where repr doesn't include quotes"""

    def __repr__(self) -> str:
        return str(self)


def _get_lambda_source_from_default_factory(source: str) -> str:
    found_lambdas: list[ast.Lambda] = []

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "default_factory" and isinstance(node.value, ast.Lambda):
            found_lambdas.append(node.value)
    if len(found_lambdas) == 1:
        return ast.unparse(found_lambdas[0])
    # These two errors are really hard to cover. Not sure if even possible, honestly :)
    elif len(found_lambdas) == 0:  # pragma: no cover
        raise InvalidGenerationInstructionError(
            f"No lambda found in default_factory even though one was passed: {source}",
        )
    else:  # pragma: no cover
        raise InvalidGenerationInstructionError(
            "More than one lambda found in default_factory. This is not supported.",
        )


class AnnotationTransformer:
    """Returns fancy and correct reprs of annotations"""

    def visit(self, value: Any):
        if PYDANTIC_V2:
            import annotated_types

            if isinstance(value, annotated_types.GroupedMetadata):
                return self.transform_grouped_metadata(value)
        if isinstance(value, list | tuple | set | frozenset):
            return self.transform_collection(value)
        if isinstance(value, dict):
            return self.transform_dict(value)
        if isinstance(value, _BaseGenericAlias | GenericAlias):
            return self.transform_generic_alias(value)
        if value is None or value is NoneType:
            return self.transform_none(value)
        if isinstance(value, type):
            return self.transform_type(value)
        if isinstance(value, Enum):
            return self.transform_enum(value)
        if isinstance(value, auto):
            return self.transform_auto(value)
        if isinstance(value, UnionType):
            return self.transform_union(value)
        if isinstance(value, LambdaType) and _LambdaFunctionName == value.__name__:
            return self.transform_lambda(value)
        if inspect.isfunction(value):
            return self.transform_function(value)
        else:
            return self.transform_other(value)

    def transform_grouped_metadata(self, value: "annotated_types.GroupedMetadata"):
        modified_fields = []
        empty_obj = type(value)
        for key in empty_obj.__dataclass_fields__:
            if getattr(value, key) != getattr(empty_obj, key):
                modified_fields.append((key, getattr(value, key)))

        return PlainRepr(
            value.__class__.__name__
            + "("
            + ", ".join(f"{PlainRepr(key)}={self.visit(v)}" for key, v in modified_fields)
            + ")",
        )

    def transform_collection(self, value: list | tuple | set | frozenset) -> Any:
        return PlainRepr(value.__class__(map(self.visit, value)))

    def transform_dict(self, value: dict) -> Any:
        return PlainRepr(
            value.__class__((self.visit(k), self.visit(v)) for k, v in value.items()),
        )

    def transform_generic_alias(self, value: _BaseGenericAlias | GenericAlias) -> Any:
        return f"{self.visit(get_origin(value))}[{', '.join(self.visit(a) for a in get_args(value))}]"

    def transform_none(self, value: NoneType) -> Any:
        return "None"

    def transform_type(self, value: type) -> Any:
        return value.__name__

    def transform_enum(self, value: Enum) -> Any:
        return PlainRepr(f"{value.__class__.__name__}.{value.name}")

    def transform_auto(self, value: auto) -> Any:
        return PlainRepr("auto()")

    def transform_union(self, value: UnionType) -> Any:
        return "typing.Union[" + (", ".join(self.visit(a) for a in get_args(value))) + "]"

    def transform_lambda(self, value: LambdaType) -> Any:
        # We clean source because getsource() can return only a part of the expression which
        # on its own is not a valid expression such as: "\n  .had(default_factory=lambda: 91)"
        return _get_lambda_source_from_default_factory(inspect.getsource(value).strip(" \n\t."))

    def transform_function(self, value: Callable) -> Any:
        return PlainRepr(value.__name__)

    def transform_other(self, value: Any) -> Any:
        return PlainRepr(repr(value))


@dataclass
class _ModelFieldWrapper:
    cls: type[BaseModel]
    annotation_ast: ast.expr | None
    name: str  # TODO: This field is duplicated here and in ModelFieldLike but it shouldn't be
    field: "ModelField | _ModelFieldLike"
    field_ast: ast.expr | None

    def get_annotation(self):  # intentionally weird to not clash with ModelField
        if self.annotation_ast:
            return PlainRepr(ast.unparse(self.annotation_ast))
        return self.field.annotation

    @property
    def field_info(self):
        if PYDANTIC_V2:
            return self.field
        else:
            return self.field.field_info


@cache
def _get_fields_from_model(cls: type):
    if not isinstance(cls, type) or not issubclass(cls, BaseModel):
        raise CodeGenerationError(f"Model {cls} is not a subclass of BaseModel")
    try:
        source = inspect.getsource(cls)
    except OSError:
        current_field_defs = {
            field_name: _ModelFieldWrapper(cls, None, field, None) for field_name, field in cls.__fields__.items()
        }
    else:
        cls_ast = cast(ast.ClassDef, ast.parse(source).body[0])
        current_field_defs = {
            node.target.id: _ModelFieldWrapper(
                cls,
                node.annotation,
                node.target.id,
                cls.__fields__[node.target.id],
                node.value,
            )
            for node in cls_ast.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in cls.__fields__
        }

    return current_field_defs


def _get_all_names_defined_on_toplevel_of_module(body: ast.Module, module_python_path: str) -> dict[str, str]:
    """Some day we will want to use this to auto-add imports for new symbols in versions. Some day..."""
    defined_names = {}
    for node in body.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            defined_names[node.name] = module_python_path
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names[target.id] = module_python_path
        elif isinstance(node, ast.ImportFrom):
            for name in node.names:
                defined_names[name.name] = _get_absolute_python_path_of_import(node, module_python_path)
        elif isinstance(node, ast.Import):
            for name in node.names:
                defined_names[name.name] = name.name
    return defined_names
