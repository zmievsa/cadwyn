import ast
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import GenericAlias, LambdaType, ModuleType, NoneType
from typing import (  # noqa: UP035
    TYPE_CHECKING,
    Any,
    List,
    get_args,
    get_origin,
)

from cadwyn._compat import (
    PYDANTIC_V2,
    is_pydantic_1_constrained_type,
)
from cadwyn._package_utils import (
    get_absolute_python_path_of_import,
)
from cadwyn._utils import PlainRepr, UnionType
from cadwyn.exceptions import CodeGenerationError, InvalidGenerationInstructionError, ModuleIsNotAvailableAsTextError

if TYPE_CHECKING:
    import annotated_types

_LambdaFunctionName = (lambda: None).__name__  # pragma: no branch
_RE_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


# A parent type of typing._GenericAlias
_BaseGenericAlias = type(List[int]).mro()[1]  # noqa: UP006

# type(list[int]) and type(List[int]) are different which is why we have to do this.
# Please note that this problem is much wider than just lists which is why we use typing._BaseGenericAlias
# instead of typing._GenericAlias.
GenericAliasUnion = GenericAlias | _BaseGenericAlias

_LambdaFunctionName = (lambda: None).__name__  # pragma: no branch


def get_fancy_repr(value: Any):
    if PYDANTIC_V2:
        import annotated_types

        if isinstance(value, annotated_types.GroupedMetadata) and hasattr(type(value), "__dataclass_fields__"):
            return transform_grouped_metadata(value)
    if isinstance(value, list | tuple | set | frozenset):
        return transform_collection(value)
    if isinstance(value, dict):
        return transform_dict(value)
    if isinstance(value, GenericAliasUnion):
        return transform_generic_alias(value)
    if value is None or value is NoneType:
        return transform_none(value)
    if isinstance(value, type):
        return transform_type(value)
    if isinstance(value, Enum):
        return transform_enum(value)
    if isinstance(value, auto):
        return transform_auto(value)
    if isinstance(value, UnionType):
        return transform_union(value)
    if isinstance(value, LambdaType) and _LambdaFunctionName == value.__name__:
        return transform_lambda(value)
    if inspect.isfunction(value):
        return transform_function(value)
    else:
        return transform_other(value)


def transform_grouped_metadata(value: "annotated_types.GroupedMetadata"):
    modified_fields = []
    empty_obj = type(value)

    for key in empty_obj.__dataclass_fields__:  # pyright: ignore[reportGeneralTypeIssues]
        if getattr(value, key) != getattr(empty_obj, key):
            modified_fields.append((key, getattr(value, key)))

    return PlainRepr(
        value.__class__.__name__
        + "("
        + ", ".join(f"{PlainRepr(key)}={get_fancy_repr(v)}" for key, v in modified_fields)
        + ")",
    )


def transform_collection(value: list | tuple | set | frozenset) -> Any:
    return PlainRepr(value.__class__(map(get_fancy_repr, value)))


def transform_dict(value: dict) -> Any:
    return PlainRepr(
        value.__class__((get_fancy_repr(k), get_fancy_repr(v)) for k, v in value.items()),
    )


def transform_generic_alias(value: GenericAliasUnion) -> Any:
    return f"{get_fancy_repr(get_origin(value))}[{', '.join(get_fancy_repr(a) for a in get_args(value))}]"


def transform_none(_: NoneType) -> Any:
    return "None"


def transform_type(value: type) -> Any:
    # This is a hack for pydantic's Constrained types
    if is_pydantic_1_constrained_type(value):
        parent = value.mro()[1]
        snake_case = _RE_CAMEL_TO_SNAKE.sub("_", value.__name__)
        cls_name = "con" + "".join(snake_case.split("_")[1:-1])
        return (
            cls_name.lower()
            + "("
            + ", ".join(
                [
                    f"{key}={get_fancy_repr(val)}"
                    for key, val in value.__dict__.items()
                    if not key.startswith("_") and val is not None and val != parent.__dict__[key]
                ],
            )
            + ")"
        )

    return value.__name__


def transform_enum(value: Enum) -> Any:
    return PlainRepr(f"{value.__class__.__name__}.{value.name}")


def transform_auto(_: auto) -> Any:
    return PlainRepr("auto()")


def transform_union(value: UnionType) -> Any:
    return "typing.Union[" + (", ".join(get_fancy_repr(a) for a in get_args(value))) + "]"


def transform_lambda(value: LambdaType) -> Any:
    # We clean source because getsource() can return only a part of the expression which
    # on its own is not a valid expression such as: "\n  .had(default_factory=lambda: 91)"
    return _get_lambda_source_from_default_factory(inspect.getsource(value).strip(" \n\t."))


def transform_function(value: Callable) -> Any:
    return PlainRepr(value.__name__)


def transform_other(value: Any) -> Any:
    return PlainRepr(repr(value))


def _get_lambda_source_from_default_factory(source: str) -> str:
    found_lambdas: list[ast.Lambda] = []

    ast.parse(source)
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


def read_python_module(module: ModuleType) -> str:
    # Can be cached in the future to gain some speedups
    try:
        return inspect.getsource(module)
    except OSError as e:
        if module.__file__ is None:  # pragma: no cover
            raise CodeGenerationError(f"Failed to get file path to the module {module}") from e
        path = Path(module.__file__)
        if path.is_file() and path.read_text() == "":
            return ""
        raise ModuleIsNotAvailableAsTextError(  # pragma: no cover
            f"Failed to get source code for module {module}. "
            "This is likely because this module is not available as code "
            "(it could be a compiled C extension or even a .pyc file). "
            "Cadwyn does not support models from such code. "
            "Please, open an issue on Cadwyn's issue tracker if you believe that your use case is valid "
            "and if you believe that it is possible for Cadwyn to support it.",
        ) from e


def get_all_names_defined_at_toplevel_of_module(body: ast.Module, module_python_path: str) -> dict[str, str]:
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
                defined_names[name.name] = get_absolute_python_path_of_import(node, module_python_path)
        elif isinstance(node, ast.Import):
            for name in node.names:
                defined_names[name.name] = name.name
    return defined_names


def add_keyword_to_call(attr_name: str, attr_value: Any, call: ast.Call):
    new_keyword = get_ast_keyword_from_argument_name_and_value(attr_name, attr_value)
    for i, keyword in enumerate(call.keywords):
        if keyword.arg == attr_name:
            call.keywords[i] = new_keyword
            break
    else:
        call.keywords.append(new_keyword)


def delete_keyword_from_call(attr_name: str, call: ast.Call):
    for i, keyword in enumerate(call.keywords):  # pragma: no branch
        if keyword.arg == attr_name:
            call.keywords.pop(i)
            break


def get_ast_keyword_from_argument_name_and_value(name: str, value: Any):
    if not isinstance(value, ast.AST):
        value = ast.parse(get_fancy_repr(value), mode="eval").body
    return ast.keyword(arg=name, value=value)


def pop_docstring_from_cls_body(cls_body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        len(cls_body) > 0
        and isinstance(cls_body[0], ast.Expr)
        and isinstance(cls_body[0].value, ast.Constant)
        and isinstance(cls_body[0].value.value, str)
    ):
        return [cls_body.pop(0)]
    else:
        return []


@dataclass(slots=True)
class _ValidatorWrapper:
    func_ast: ast.FunctionDef
    index_of_validator_decorator: int
    field_names: set[str | ast.expr] | None
    is_deleted: bool = False


def get_validator_info_or_none(method: ast.FunctionDef) -> _ValidatorWrapper | None:
    for index, decorator in enumerate(method.decorator_list):
        # The cases we handle here:
        # * `Name(id="root_validator")`
        # * `Call(func=Name(id="validator"), args=[Constant(value="foo")])`
        # * `Attribute(value=Name(id="pydantic"), attr="root_validator")`
        # * `Call(func=Attribute(value=Name(id="pydantic"), attr="root_validator"), args=[])`

        if isinstance(decorator, ast.Call) and ast.unparse(decorator.func).endswith("validator"):
            if len(decorator.args) == 0:
                return _ValidatorWrapper(method, index, None)
            else:
                return _ValidatorWrapper(
                    method, index, {arg.value if isinstance(arg, ast.Constant) else arg for arg in decorator.args}
                )
        elif isinstance(decorator, ast.Name | ast.Attribute) and ast.unparse(decorator).endswith("validator"):
            return _ValidatorWrapper(method, index, None)
    return None
