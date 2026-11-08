import ast
import cmath
import dataclasses
import inspect
import math
import sys
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable
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
_BUILTIN_CONTAINERS = (list, tuple, set, frozenset, dict)
_STANDARD_MUTABLE_CONTAINERS = (list, set, dict, OrderedDict, Counter, defaultdict)
_IDENTICAL_REPRS_ERROR = (
    "Cannot render {} whose distinct items have identical representations. Use a named default_factory instead."
)


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
    lambda_keyword: str = "default_factory"


def get_fancy_repr(value: Any, context: Union[_ReprContext, None] = None) -> Any:
    if context is None:
        context = _ReprContext()
    if not isinstance(value, list | tuple | set | frozenset | dict):
        return _get_fancy_repr(value, context)
    if id(value) in context.active:
        raise CadwynRenderError("Cannot render recursive containers faithfully. Use a named default_factory instead.")
    if type(value) in _STANDARD_MUTABLE_CONTAINERS:
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


def _float_repr(value: float) -> PlainRepr:
    return PlainRepr(repr(value) if math.isfinite(value) else f"float({str(value)!r})")


def _get_fancy_repr(value: Any, context: _ReprContext) -> Any:
    if type(value) is float and not math.isfinite(value):
        return _float_repr(value)
    if type(value) is complex and not cmath.isfinite(value):
        return PlainRepr(f"complex({_float_repr(value.real)}, {_float_repr(value.imag)})")
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
        return transform_lambda(value, context.lambda_keyword)
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
    if _is_generated_namedtuple(cls):
        # Named tuples take their fields as separate arguments rather than as one iterable
        fields = (_container_item_repr(item, context) for item in tuple.__iter__(cast("tuple[object, ...]", value)))
        return PlainRepr(f"{cls.__name__}({', '.join(fields)})")
    items = [_container_item_repr(item, context) for item in value]
    if isinstance(value, set | frozenset) and len(set(items)) != len(items):
        raise CadwynRenderError(_IDENTICAL_REPRS_ERROR.format(cls.__qualname__))
    expected = set(items) if isinstance(value, set | frozenset) else items
    return _rebuild_and_repr(value, lambda: cls(iter(items)), expected)


def _is_generated_namedtuple(cls: type) -> bool:
    # Only the classes that namedtuple() and NamedTuple generate are known to take their fields positionally.
    # Their subclasses can take other arguments, so they render through their constructor and repr instead.
    return (
        cls.__bases__ == (tuple,)
        and isinstance(vars(cls).get("_fields"), tuple)
        and "__new__" in vars(cls)
        and getattr(cls.__new__, "__globals__", {}).get("_tuple_new") is tuple.__new__
        and cls.__init__ is object.__init__
    )


def _mapping_repr(value: dict[object, object], context: _ReprContext) -> dict[PlainRepr, PlainRepr]:
    mapping = {_container_item_repr(k, context): _container_item_repr(v, context) for k, v in dict.items(value)}
    if len(mapping) != len(value):
        raise CadwynRenderError(_IDENTICAL_REPRS_ERROR.format(type(value).__qualname__))
    return mapping


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
        return PlainRepr(f"defaultdict({_container_item_repr(factory, context)}, {_mapping_repr(value, context)!r})")
    if cls is Counter:
        # Counter(iterable_of_pairs) would count the pairs, and Counter's repr would sort the counts
        return PlainRepr(f"Counter({_mapping_repr(value, context)!r})" if value else "Counter()")
    pairs = [(_container_item_repr(k, context), _container_item_repr(v, context)) for k, v in value.items()]
    if len({key for key, _ in pairs}) != len(pairs):
        raise CadwynRenderError(_IDENTICAL_REPRS_ERROR.format(cls.__qualname__))
    return _rebuild_and_repr(value, lambda: cls(iter(pairs)), pairs)


def _stored_items(container: object) -> object:
    if isinstance(container, dict):
        return list(dict.items(container))
    if isinstance(container, list):
        return list(list.__iter__(container))
    if isinstance(container, tuple):
        return list(tuple.__iter__(container))
    if isinstance(container, set):
        return set(set.__iter__(container))
    return set(frozenset.__iter__(cast("frozenset[object]", container)))


def _rebuild_and_repr(original: object, rebuild: Callable[[], object], expected: object) -> PlainRepr:
    cls = type(original)
    try:
        rebuilt = rebuild()
        source = repr(rebuilt)
        expression = ast.parse(source, mode="eval").body
    except Exception as error:  # User constructors and reprs can fail in arbitrary ways.
        raise CadwynRenderError(
            f"Cannot render {cls.__qualname__} through its constructor and repr. Use a named default_factory instead."
        ) from error
    if type(rebuilt) is not cls or _stored_items(rebuilt) != expected:
        # E.g. a Counter subclass counts the (key, value) pairs that we pass to its constructor
        raise CadwynRenderError(
            f"Cannot render {cls.__qualname__} because its constructor doesn't keep its type and items. "
            "Use a named default_factory instead."
        )
    if cls not in _BUILTIN_CONTAINERS and isinstance(expression, ast.List | ast.Tuple | ast.Dict | ast.Set):
        raise CadwynRenderError(
            f"Cannot render container subclass {cls.__qualname__} as a builtin literal "
            "without losing its type. Use a named default_factory instead."
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


def transform_lambda(value: LambdaType, keyword: str = "default_factory") -> Any:
    if keyword != "default_factory" and value.__closure__:
        raise CadwynRenderError("Cannot inline a config lambda with captured variables. Use a named function instead.")
    # We clean source because getsource() can return only a part of the expression which
    # on its own is not a valid expression such as: "\n  .had(default_factory=lambda: 91)"
    return _get_lambda_source_from_keyword(inspect.getsource(value).strip(" \n\t."), keyword)


def transform_function(value: FunctionType) -> Any:
    return PlainRepr(value.__name__)


def transform_other(value: Any) -> Any:
    return PlainRepr(repr(value))


def _get_lambda_source_from_keyword(source: str, keyword: str) -> str:
    tree = ast.parse(source)
    found_lambdas: list[ast.Lambda] = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == keyword and isinstance(node.value, ast.Lambda)
    ]
    found_lambdas += [
        value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == keyword and isinstance(value, ast.Lambda)
    ]

    if len(found_lambdas) == 1:
        return ast.unparse(found_lambdas[0])
    # E.g. `EXTRA = lambda s: ...` passed as `json_schema_extra=EXTRA`
    elif len(found_lambdas) == 0:
        raise InvalidGenerationInstructionError(
            f"No lambda found in {keyword} even though one was passed: {source}",
        )
    else:  # pragma: no cover # Really hard to cover. Not sure if even possible, honestly :)
        raise InvalidGenerationInstructionError(
            f"More than one lambda found in {keyword}. This is not supported.",
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
