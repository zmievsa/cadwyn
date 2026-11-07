import ast
import dataclasses
import inspect
import sys
from collections import Counter, OrderedDict, defaultdict
from enum import Enum, auto
from types import FunctionType, GenericAlias, LambdaType
from typing import (  # noqa: UP035
    Any,
    List,
    Union,
    cast,
    get_args,
    get_origin,
)

import annotated_types

from cadwyn._utils import PlainRepr, UnionType
from cadwyn.exceptions import CadwynRenderError, InvalidGenerationInstructionError

_LambdaFunctionName = (lambda: None).__name__  # pragma: no branch
NoneType = type(None)


# A parent type of typing._GenericAlias
_BaseGenericAlias: type = cast("type", type(List[int])).mro()[1]  # noqa: UP006

# type(list[int]) and type(List[int]) are different which is why we have to do this.
# Please note that this problem is much wider than just lists which is why we use typing._BaseGenericAlias
# instead of typing._GenericAlias.
GenericAliasUnion = Union[GenericAlias, _BaseGenericAlias]  # ty: ignore[invalid-type-form]
GenericAliasUnionArgs = get_args(GenericAliasUnion)
if sys.version_info >= (3, 14):  # pragma: no cover
    # In Python 3.14, Union types are no longer subtypes of _BaseGenericAlias
    GenericAliasUnionArgs = (*GenericAliasUnionArgs, type(Union[int, str]))


def get_fancy_repr(value: Any, seen_containers: Union[set[int], None] = None) -> Any:
    if seen_containers is None:
        seen_containers = set()
    if isinstance(value, list | tuple | set | frozenset | dict):
        if id(value) in seen_containers:
            raise CadwynRenderError(
                "Cannot render recursive or shared containers faithfully. Use a named default_factory instead."
            )
        seen_containers.add(id(value))
    if isinstance(value, annotated_types.GroupedMetadata) and dataclasses.is_dataclass(value):
        return transform_grouped_metadata(value, dataclasses.fields(value), seen_containers)
    if isinstance(value, list | tuple | set | frozenset):
        return transform_collection(value, seen_containers)
    if isinstance(value, dict):
        return transform_dict(value, seen_containers)
    if isinstance(value, UnionType):
        return transform_union(value, seen_containers)
    if isinstance(value, GenericAliasUnionArgs):
        return transform_generic_alias(value, seen_containers)
    if value is None or value is NoneType:
        return transform_none(value)
    if isinstance(value, type):
        return transform_type(value)
    if isinstance(value, Enum):
        return transform_enum(value)
    if isinstance(value, auto):  # pragma: no cover # it works but we no longer use auto
        return transform_auto(value)
    if isinstance(value, LambdaType) and _LambdaFunctionName == value.__name__:
        return transform_lambda(value)
    if isinstance(value, FunctionType):
        return transform_function(value)
    else:
        return transform_other(value)


def transform_grouped_metadata(
    value: "annotated_types.GroupedMetadata", fields: tuple[dataclasses.Field[Any], ...], seen_containers: set[int]
) -> PlainRepr:
    empty_obj = type(value)

    modified_fields = [
        (key, getattr(value, key))
        for key in (field.name for field in fields)
        if getattr(value, key) != getattr(empty_obj, key)
    ]

    return PlainRepr(
        value.__class__.__name__
        + "("
        + ", ".join(f"{PlainRepr(key)}={get_fancy_repr(v, seen_containers)}" for key, v in modified_fields)
        + ")",
    )


def transform_collection(
    value: Union[list[object], tuple[object, ...], set[object], frozenset[object]],
    seen_containers: set[int],
) -> PlainRepr:
    cls = type(value)
    if cls in {list, tuple, set, frozenset}:
        items = ", ".join(get_fancy_repr(item, seen_containers) for item in value)
        if cls is list:
            return PlainRepr(f"[{items}]")
        if cls is tuple:
            return PlainRepr(f"({items},)" if items else "()")
        set_source = f"{{{items}}}" if items else "set()"
        return PlainRepr(set_source if cls is set else f"frozenset({set_source})")
    if (
        isinstance(value, tuple)
        and cls.__bases__ == (tuple,)
        and getattr(cls, "__slots__", None) == ()
        and isinstance(getattr(cls, "_fields", None), tuple)
    ):
        # Bypass __new__: a named tuple can override it to change the supplied values.
        items = ", ".join(get_fancy_repr(item, seen_containers) for item in tuple.__iter__(value))
        tuple_source = f"({items},)" if items else "()"
        return PlainRepr(f"tuple.__new__({cls.__name__}, {tuple_source})")
    raise _unsupported_container(value)


def transform_dict(value: dict[object, object], seen_containers: set[int]) -> PlainRepr:
    cls = type(value)
    if cls not in {dict, defaultdict, OrderedDict, Counter}:
        raise _unsupported_container(value)
    if getattr(value, "__dict__", {}):
        raise CadwynRenderError(
            f"Cannot render {cls.__name__} with instance attributes faithfully. Use a named default_factory instead."
        )
    items = [
        (get_fancy_repr(key, seen_containers), get_fancy_repr(item, seen_containers)) for key, item in value.items()
    ]
    if cls is OrderedDict:
        pairs = ", ".join(f"({key}, {item})" for key, item in items)
        return PlainRepr(f"OrderedDict([{pairs}])")
    mapping = "{" + ", ".join(f"{key}: {item}" for key, item in items) + "}"
    if isinstance(value, defaultdict):
        factory = value.default_factory
        if factory is not None and not (
            isinstance(factory, type) or (isinstance(factory, FunctionType) and factory.__name__ != _LambdaFunctionName)
        ):
            raise CadwynRenderError(
                "Cannot render defaultdict factory faithfully. Use a named default_factory for the whole field instead."
            )
        return PlainRepr(f"defaultdict({get_fancy_repr(factory, seen_containers)}, {mapping})")
    if cls is Counter:
        return PlainRepr(f"Counter({mapping})")
    return PlainRepr(mapping)


def _unsupported_container(value: object) -> CadwynRenderError:
    return CadwynRenderError(
        f"Cannot render container subclass {type(value).__qualname__} faithfully. Use a named default_factory instead."
    )


def transform_generic_alias(value: object, seen_containers: set[int]) -> str:
    origin = get_fancy_repr(get_origin(value), seen_containers)
    args = ", ".join(get_fancy_repr(arg, seen_containers) for arg in get_args(value))
    return f"{origin}[{args}]"


def transform_none(value: Union[type[None], None]) -> str:
    return "None" if value is None else "type(None)"


def transform_type(value: type) -> Any:
    return value.__name__


def transform_enum(value: Enum) -> Any:
    return PlainRepr(f"{value.__class__.__name__}.{value.name}")


def transform_auto(_: auto) -> Any:  # pragma: no cover # it works but we no longer use auto
    return PlainRepr("auto()")


def transform_union(value: Any, seen_containers: set[int]) -> str:
    return "typing.Union[" + (", ".join(get_fancy_repr(a, seen_containers) for a in get_args(value))) + "]"


def transform_lambda(value: LambdaType) -> Any:
    # We clean source because getsource() can return only a part of the expression which
    # on its own is not a valid expression such as: "\n  .had(default_factory=lambda: 91)"
    return _get_lambda_source_from_default_factory(inspect.getsource(value).strip(" \n\t."))


def transform_function(value: FunctionType) -> Any:
    return PlainRepr(value.__name__)


def transform_other(value: Any) -> Any:
    return PlainRepr(repr(value))


def _get_lambda_source_from_default_factory(source: str) -> str:
    found_lambdas: list[ast.Lambda] = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.keyword) and node.arg == "default_factory" and isinstance(node.value, ast.Lambda)
    ]

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
