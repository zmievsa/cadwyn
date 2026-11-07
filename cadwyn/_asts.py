import ast
import dataclasses
import inspect
import sys
from collections import Counter, OrderedDict, defaultdict
from enum import Enum, auto
from functools import partial
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
_BUILTIN_COLLECTION_IDS = frozenset(map(id, (list, tuple, set, frozenset)))
_BUILTIN_CONTAINER_IDS = _BUILTIN_COLLECTION_IDS | {id(dict)}
_STANDARD_MAPPING_IDS = frozenset(map(id, (dict, OrderedDict, Counter)))
_STANDARD_MUTABLE_CONTAINER_IDS = frozenset(map(id, (list, set, dict, OrderedDict, Counter, defaultdict)))


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


@dataclasses.dataclass
class _ReprContext:
    active: set[int] = dataclasses.field(default_factory=set)
    mutable: dict[int, object] = dataclasses.field(default_factory=dict)


def get_fancy_repr(value: Any, context: Union[_ReprContext, None] = None) -> Any:
    if context is None:
        context = _ReprContext()
    if not isinstance(value, list | tuple | set | frozenset | dict):
        return _get_fancy_repr(value, context)
    if id(value) in context.active:
        raise CadwynRenderError("Cannot render recursive containers faithfully. Use a named default_factory instead.")
    if id(type(value)) in _STANDARD_MUTABLE_CONTAINER_IDS:
        if id(value) in context.mutable:
            raise CadwynRenderError(
                "Cannot render shared mutable containers faithfully. Use a named default_factory instead."
            )
        # Keep temporary values from subclass iterators alive so their ids cannot be reused during this rendering.
        context.mutable[id(value)] = value
    context.active.add(id(value))
    try:
        return _get_fancy_repr(value, context)
    finally:
        context.active.remove(id(value))


def _get_fancy_repr(value: Any, context: _ReprContext) -> Any:
    if isinstance(value, annotated_types.GroupedMetadata) and dataclasses.is_dataclass(value):
        return transform_grouped_metadata(value, dataclasses.fields(value), context)
    if isinstance(value, list | tuple | set | frozenset):
        return transform_collection(value, context)
    if isinstance(value, dict):
        return transform_dict(value, context)
    if isinstance(value, UnionType):
        return transform_union(value, context)
    if isinstance(value, GenericAliasUnionArgs):
        return transform_generic_alias(value, context)
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
    value: "annotated_types.GroupedMetadata", fields: tuple[dataclasses.Field[Any], ...], context: _ReprContext
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
        + ", ".join(f"{PlainRepr(key)}={get_fancy_repr(v, context)}" for key, v in modified_fields)
        + ")",
    )


def _container_item_repr(value: Any, context: _ReprContext) -> PlainRepr:
    # Values are expressions rather than quoted strings. Annotations still spell NoneType as None.
    return PlainRepr("type(None)" if value is NoneType else get_fancy_repr(value, context))


def transform_collection(
    value: Union[list[object], tuple[object, ...], set[object], frozenset[object]],
    context: _ReprContext,
) -> PlainRepr:
    cls = type(value)
    try:
        # Keep main's constructor and repr, including its ordering and formatting, for successful renderings.
        converter = _container_item_repr if id(cls) in _BUILTIN_COLLECTION_IDS else get_fancy_repr
        items = map(partial(converter, context=context), value)
        return _container_repr(value, value.__class__(items))
    except (TypeError, SyntaxError) as error:
        if not (
            isinstance(value, tuple)
            and cls.__bases__ == (tuple,)
            and getattr(cls, "__slots__", None) == ()
            and isinstance(getattr(cls, "_fields", None), tuple)
        ):
            raise CadwynRenderError(
                f"Cannot render {cls.__qualname__} through its constructor and repr. "
                "Use a named default_factory instead."
            ) from error
    # NamedTuple's positional constructor cannot take one iterable. Bypass it to retain the stored tuple values.
    items_repr = tuple(_container_item_repr(item, context) for item in tuple.__iter__(value))
    return PlainRepr(f"tuple.__new__({cls.__name__}, {items_repr!r})")


def transform_dict(value: dict[object, object], context: _ReprContext) -> PlainRepr:
    cls = type(value)
    if (cls is OrderedDict or cls is Counter) and vars(value):
        raise CadwynRenderError(
            f"Cannot render {cls.__name__} with instance attributes faithfully. Use a named default_factory instead."
        )
    if cls is defaultdict and isinstance(value, defaultdict):
        factory = value.default_factory
        if factory is not None and not (
            isinstance(factory, type) or (isinstance(factory, FunctionType) and factory.__name__ != _LambdaFunctionName)
        ):
            raise CadwynRenderError(
                "Cannot render defaultdict factory faithfully. Use a named default_factory for the whole field instead."
            )
        mapping = {_container_item_repr(k, context): _container_item_repr(v, context) for k, v in value.items()}
        return PlainRepr(f"defaultdict({_container_item_repr(factory, context)}, {mapping!r})")
    if cls is Counter and value:
        # Counter(iterable_of_pairs) counts the pairs instead of retaining the mapping's counts.
        mapping = {_container_item_repr(k, context): _container_item_repr(v, context) for k, v in value.items()}
        return PlainRepr(f"Counter({mapping!r})")
    items = (
        (_container_item_repr(k, context), _container_item_repr(v, context))
        if id(cls) in _STANDARD_MAPPING_IDS
        else (get_fancy_repr(k, context), get_fancy_repr(v, context))
        for k, v in value.items()
    )
    return _container_repr(value, value.__class__(items))


def _container_repr(original: object, rebuilt: object) -> PlainRepr:
    source = repr(rebuilt)
    expression = ast.parse(source, mode="eval").body
    if id(type(original)) not in _BUILTIN_CONTAINER_IDS and isinstance(
        expression, ast.List | ast.Tuple | ast.Dict | ast.Set
    ):
        raise CadwynRenderError(
            f"Cannot render container subclass {type(original).__qualname__} as a builtin literal "
            "without losing its type. "
            "Use a named default_factory instead."
        )
    return PlainRepr(source)


def transform_generic_alias(value: object, context: _ReprContext) -> str:
    origin = get_fancy_repr(get_origin(value), context)
    args = ", ".join(get_fancy_repr(arg, context) for arg in get_args(value))
    return f"{origin}[{args}]"


def transform_none(_: Union[type[None], None]) -> Any:
    return "None"


def transform_type(value: type) -> Any:
    return value.__name__


def transform_enum(value: Enum) -> Any:
    return PlainRepr(f"{value.__class__.__name__}.{value.name}")


def transform_auto(_: auto) -> Any:  # pragma: no cover # it works but we no longer use auto
    return PlainRepr("auto()")


def transform_union(value: Any, context: _ReprContext) -> str:
    return "typing.Union[" + (", ".join(get_fancy_repr(a, context) for a in get_args(value))) + "]"


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
