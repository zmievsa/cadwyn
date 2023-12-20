import ast
import inspect
import re
from collections.abc import Callable
from enum import Enum, auto
from types import GenericAlias, LambdaType, NoneType
from typing import (
    TYPE_CHECKING,
    Any,
    _BaseGenericAlias,  # pyright: ignore[reportGeneralTypeIssues]
    get_args,
    get_origin,
)

from cadwyn._compat import (
    PYDANTIC_V2,
    get_attrs_that_are_not_from_field_and_that_are_from_field,
    is_pydantic_constrained_type,
)
from cadwyn._utils import UnionType
from cadwyn.exceptions import InvalidGenerationInstructionError

if TYPE_CHECKING:
    import annotated_types
_LambdaFunctionName = (lambda: None).__name__  # pragma: no branch
_RE_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


def get_fancy_repr(value: Any):
    if PYDANTIC_V2:
        import annotated_types

        if isinstance(value, annotated_types.GroupedMetadata):
            return transform_grouped_metadata(value)
    if isinstance(value, list | tuple | set | frozenset):
        return transform_collection(value)
    if isinstance(value, dict):
        return transform_dict(value)
    if isinstance(value, _BaseGenericAlias | GenericAlias):
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
    for key in empty_obj.__dataclass_fields__:
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


def transform_generic_alias(value: _BaseGenericAlias | GenericAlias) -> Any:
    return f"{get_fancy_repr(get_origin(value))}[{', '.join(get_fancy_repr(a) for a in get_args(value))}]"


def transform_none(_: NoneType) -> Any:
    return "None"


def transform_type(value: type) -> Any:
    # This is a hack for pydantic's Constrained types
    if is_pydantic_constrained_type(value):
        if get_attrs_that_are_not_from_field_and_that_are_from_field(value)[0]:
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
        else:
            # In pydantic V1:
            # MRO of conint looks like: []
            value = value.mro()[-2]

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
    return _find_a_lambda(inspect.getsource(value).strip(" \n\t."))


def transform_function(value: Callable) -> Any:
    return PlainRepr(value.__name__)


def transform_other(value: Any) -> Any:
    return PlainRepr(repr(value))


class PlainRepr(str):
    """String class where repr doesn't include quotes"""

    def __repr__(self) -> str:
        return str(self)


def _find_a_lambda(source: str) -> str:
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
