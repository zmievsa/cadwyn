import collections.abc
import copy
import decimal
import sys
import types
import typing
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import (
    Any,
    ForwardRef,
    NamedTuple,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from pydantic._internal._config import ConfigWrapper
from pydantic._internal._fields import rebuild_model_fields
from pydantic._internal._generics import get_model_typevars_map
from pydantic._internal._model_construction import unpack_lenient_weakvaluedict
from pydantic._internal._namespace_utils import NsResolver
from pydantic._internal._typing_extra import try_eval_type as pydantic_try_eval_type
from pydantic.fields import FieldInfo
from typing_extensions import (
    TypeVar,
    _AnnotatedAlias,
)

from cadwyn._asts import GenericAliasUnionArgs
from cadwyn._utils import (
    UnionType,
    _get_type_alias_value,
    lenient_issubclass,
)


def _rebuild_model(model: type[BaseModel]) -> None:
    # In case we have a forwardref within one of the fields
    # For example, when "from __future__ import annotations" is used in the file with the schema
    if model is not BaseModel:
        # An empty namespace makes sure that our local variables cannot be used to resolve the forward refs
        model.model_rebuild(raise_errors=False, _types_namespace={})


def _get_annotations_without_evaluating(obj: Any) -> dict[str, Any]:
    """Return the annotations of a class or a function, keeping the ones that cannot be evaluated as forward refs"""
    if sys.version_info >= (3, 14):  # pragma: no cover
        from annotationlib import Format, get_annotations

        # Annotations are evaluated lazily since Python 3.14 so the names that are only imported for type checking
        # would raise a NameError
        return get_annotations(obj, format=Format.FORWARDREF)
    # E.g. `object` has none before Python 3.14, which annotationlib treats as no annotations
    return getattr(obj, "__annotations__", {})


_UNRESOLVED: Any = object()
_T_EVALUATED = TypeVar("_T_EVALUATED")


def _evaluate_user_annotations(evaluate: Callable[[], _T_EVALUATED]) -> _T_EVALUATED:
    """Evaluate the annotations of the user's code for reference discovery, returning _UNRESOLVED if they fail.

    Discovery evaluates the annotations of models that main may never copy, e.g. the ones in untouched generic args.
    Any exception from evaluating them, such as a NameError for a name that is not defined yet, a TypeError for
    a generic that only exists in stubs (`pandas.Series[int]`, Django's `QuerySet[Model]`) or an AttributeError,
    means that discovery cannot know which models copying would request. The generic args that lead to them then
    keep their head models, which is exactly main's behavior, so catching every exception here is safe. Only the
    evaluation of the user's annotations goes through here so the bugs in Cadwyn's own logic still surface.
    """
    try:
        return evaluate()
    except RecursionError:
        # This is more likely a bug in Cadwyn than in the user's annotations
        raise
    except Exception:  # noqa: BLE001
        return _UNRESOLVED


def _has_undefined_names_in_lazy_annotations(obj: Any) -> bool:
    """Whether the annotations of a class or a function refer to names that are not defined yet.

    Since Python 3.14, annotations are evaluated lazily and _get_annotations_without_evaluating keeps such names
    as forward refs, which would resolve to other objects once the names get defined. Earlier versions evaluate
    annotations when they are defined unless they are strings, which stay strings.
    """
    if sys.version_info >= (3, 14):  # pragma: no cover
        from annotationlib import Format, get_annotations

        return _evaluate_user_annotations(lambda: get_annotations(obj, format=Format.VALUE)) is _UNRESOLVED
    return False


# The models whose copies do not redefine inherited fields (see _ReferenceCycles.get_redefined_fields)
_NO_REDEFINED_FIELDS: "collections.abc.Mapping[str, Any]" = types.MappingProxyType({})


def _get_model_annotations(
    model: type[BaseModel],
    fields: Union[dict[str, FieldInfo], None] = None,
    redefined_fields: "collections.abc.Mapping[str, Any]" = _NO_REDEFINED_FIELDS,
) -> dict[str, Any]:
    fields = model.model_fields if fields is None else fields

    def _rebuild_annotated(name: str, value: Any):
        if field_info := fields.get(name):
            return _get_annotation_with_metadata(field_info)
        return value  # pragma: no cover

    # Pydantic has already resolved the annotations of the fields so we prefer them over unresolved annotations
    annotations = {
        name: _rebuild_annotated(name, value) if _is_unresolved_annotation(value) else value
        for name, value in _get_annotations_without_evaluating(model).items()
    }
    # The inherited fields that the copy redefines (see _ReferenceCycles.get_redefined_fields)
    return annotations | dict(redefined_fields)


def _is_unresolved_annotation(annotation: Any) -> bool:
    """Whether the annotation is a string or a forward ref that annotationlib created for undefined names.

    Forward refs that the user wrote (e.g. `ForwardRef("Model")` in create_model) stay as they are, as on main.
    """
    if isinstance(annotation, str):
        return True
    return isinstance(annotation, ForwardRef) and getattr(annotation, "__owner__", None) is not None


def _get_model_field_names(
    model: type[BaseModel],
    fields: Union[dict[str, FieldInfo], None] = None,
    redefined_field_names: "collections.abc.Iterable[str]" = (),
) -> list[str]:
    fields = model.model_fields if fields is None else fields
    names = [name for name in _get_annotations_without_evaluating(model) if name in fields]
    return names + [name for name in redefined_field_names if name not in names]


def _get_rebuilt_model_fields(model: type[BaseModel]) -> Union[dict[str, FieldInfo], None]:
    """Return the fields that _rebuild_model would give the model without rebuilding it, if they can be resolved.

    model_rebuild first resolves the fields with the same pydantic function and namespaces. Unlike model_rebuild,
    it does not change the model, build its schema, or call its hooks.
    """
    if model.__pydantic_fields_complete__:
        return model.model_fields
    ns_resolver = NsResolver(parent_namespace=unpack_lenient_weakvaluedict(model.__pydantic_parent_namespace__) or {})
    typevars_map = get_model_typevars_map(model)
    config_wrapper = ConfigWrapper(model.model_config, check=False)
    # Pydantic evaluates the annotations of the fields in there
    rebuilt_fields = _evaluate_user_annotations(
        lambda: rebuild_model_fields(
            model, config_wrapper=config_wrapper, ns_resolver=ns_resolver, typevars_map=typevars_map
        )
    )
    if rebuilt_fields is _UNRESOLVED:
        # E.g. some names in the annotations are not defined yet
        return None
    # Pydantic >= 2.13 also returns the info about the extra fields
    return rebuilt_fields[0] if isinstance(rebuilt_fields, tuple) else rebuilt_fields


# The modules of the generics whose args are types that pydantic resolves, such as list, dict, Optional, or Callable
_TYPE_POSITION_GENERIC_MODULES = frozenset(
    {"builtins", "collections", "collections.abc", "types", "typing", "typing_extensions"}
)


def _is_type_position_in_generic(origin: Any, index: int) -> bool:
    """Whether the arg of the generic at the index is a type that pydantic resolves if it is a forward ref.

    Other generics, such as the ones with a custom __class_getitem__, may need the classes themselves.
    The args of Literal are values and the metadata of Annotated can be anything.
    """
    from typing_inspection.typing_objects import is_annotated, is_literal

    if is_annotated(origin):
        return index == 0
    if is_literal(origin):
        return False
    return getattr(origin, "__module__", None) in _TYPE_POSITION_GENERIC_MODULES


def _rebuild_generic_alias(annotation: Any, new_args: tuple[Any, ...]) -> Any:
    """Rebuild a generic alias such as list[Item] or Optional[Item] with new args"""
    origin = get_origin(annotation)
    # A bare `typing.Callable` has nothing to version and cannot be rebuilt as `Callable[()]`
    if origin is collections.abc.Callable and not new_args:
        return annotation
    # The special forms that take a single arg (e.g. ClassVar, Final, TypeGuard, and Unpack) reject a tuple of it.
    # A single arg is also what the user's code passed to any other generic, e.g. `list[Item]`
    if len(new_args) == 1:
        return origin[new_args[0]]
    return origin[new_args]


def _copy_field_info(field_info: FieldInfo) -> FieldInfo:
    # The same as pydantic's FieldInfo._copy() (which does not exist in older versions of pydantic):
    # the mutable attributes must not be shared with the original either
    field_info_copy = copy.copy(field_info)
    for attr_name in ("metadata", "_attributes_set", "_qualifiers"):
        setattr(field_info_copy, attr_name, copy.copy(getattr(field_info, attr_name)))
    return field_info_copy


def _replace_forward_refs(annotation: Any, models_by_name: dict[str, type[BaseModel]]) -> Any:
    """Replace the forward refs to the models in the type positions of the annotation (see _CycleCopier)"""
    return _replace_in_type_positions(
        annotation,
        lambda value: models_by_name.get(value.__forward_arg__, value) if isinstance(value, ForwardRef) else value,
    )


def _replace_in_type_positions(annotation: Any, replace: Callable[[Any], Any]) -> Any:
    """Replace the values in the type positions of the annotation, keeping the annotation if nothing changes"""
    replaced = replace(annotation)
    if replaced is not annotation:
        return replaced
    if isinstance(annotation, list):
        # E.g. the args of Callable[[Item], None]
        return [_replace_in_type_positions(arg, replace) for arg in annotation]
    if not isinstance(annotation, (*GenericAliasUnionArgs, UnionType)):
        return annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    new_args = tuple(
        _replace_in_type_positions(arg, replace) if _is_type_position_in_generic(origin, index) else arg
        for index, arg in enumerate(args)
    )
    if all(new_arg is arg for new_arg, arg in zip(new_args, args, strict=True)):
        return annotation
    if isinstance(annotation, UnionType):
        return typing.Union[new_args]
    return _rebuild_generic_alias(annotation, new_args)


def _get_extra_fields_annotation(model: type[BaseModel]) -> Any:
    """Return the annotation of `__pydantic_extra__` that types the extra fields of the model, if any"""
    for cls in model.__mro__:
        if not lenient_issubclass(cls, BaseModel):
            continue
        annotation = _get_annotations_without_evaluating(cls).get("__pydantic_extra__")
        if annotation is not None:
            return _try_eval_type_for_discovery(annotation, _get_module_globals(cls))
    return None


def _get_module_globals(cls: type) -> dict[str, Any]:
    module = sys.modules.get(cls.__module__)
    return {} if module is None else vars(module)


_BUILTIN_TYPES_OF_RECURSIVE_TYPE_ALIASES: "tuple[Any, ...]" = (
    *(str, int, float, bool, bytes, None, type(None), Ellipsis),
    *(decimal.Decimal, datetime, date, time, timedelta, uuid.UUID),
)
_BUILTIN_GENERICS_OF_RECURSIVE_TYPE_ALIASES: "tuple[Any, ...]" = (
    *(list, dict, set, frozenset, tuple, typing.Union, types.UnionType),
)


def _contains_only_builtin_types(annotation: Any, *, visited_aliases: "set[int]", alias_name: str = "") -> bool:
    """Whether the annotation only contains builtin types and type aliases whose values only contain them too.

    This is an allow-list: everything else (e.g. classes, Annotated metadata, or forward refs to anything but the
    type alias that contains them) could refer to models or enums that the versions change.
    """
    from typing_inspection.typing_objects import is_any, is_literal, is_typealiastype

    if is_typealiastype(annotation):
        if id(annotation) in visited_aliases:
            return True
        visited_aliases.add(id(annotation))
        value = _evaluate_user_annotations(lambda: _get_type_alias_value(annotation))
        return value is not _UNRESOLVED and _contains_only_builtin_types(
            value, visited_aliases=visited_aliases, alias_name=annotation.__name__
        )
    if isinstance(annotation, (str, ForwardRef)):
        # Pydantic resolves the name of a type alias within its value to the type alias itself
        return (annotation if isinstance(annotation, str) else annotation.__forward_arg__) == alias_name
    if is_any(annotation) or _is_one_of(annotation, _BUILTIN_TYPES_OF_RECURSIVE_TYPE_ALIASES):
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if is_literal(origin):
        return all(type(arg) in (str, int, bool, bytes, type(None)) for arg in args)
    return _is_one_of(origin, _BUILTIN_GENERICS_OF_RECURSIVE_TYPE_ALIASES) and all(
        _contains_only_builtin_types(arg, visited_aliases=visited_aliases, alias_name=alias_name) for arg in args
    )


def _is_one_of(value: Any, members: "tuple[Any, ...]") -> bool:
    # By identity because the annotations can be anything, e.g. unhashable or with an __eq__ that raises
    return any(value is member for member in members)


class _TypeAliasRefersToItselfError(Exception):
    """Raised within the copy of a type alias that refers to itself, up to where we started copying it"""

    def __init__(self, alias: Any) -> None:
        super().__init__(alias)
        self.alias = alias


class _TypeAliasValue(NamedTuple):
    alias: Any
    args: tuple[Any, ...]
    value: Any


def _are_same_args(args: tuple[Any, ...], other_args: tuple[Any, ...]) -> bool:
    try:
        return bool(args == other_args)
    except Exception:  # noqa: BLE001  # pragma: no cover # E.g. Annotated metadata whose __eq__ raises
        return False


def _get_value_of_type_alias_with_args(annotation: Any) -> "Union[_TypeAliasValue, Any]":
    """Return the type alias and its value with the args of the annotation, or _UNRESOLVED if we cannot.

    E.g. `list[int]` for `Ints[int]` with `type Ints[T] = list[T]`, or for `Ints` with `type Ints = list[int]`.
    """
    from typing_inspection.typing_objects import is_typealiastype

    if is_typealiastype(annotation):
        alias, args = annotation, ()
    elif isinstance(annotation, GenericAliasUnionArgs) and is_typealiastype(get_origin(annotation)):
        alias, args = get_origin(annotation), get_args(annotation)
    else:
        return _UNRESOLVED
    try:
        args_by_param = dict(zip(alias.__type_params__, args, strict=True))
        value = _get_type_alias_value(alias)
        if isinstance(value, TypeVar):
            value = args_by_param[value]
        elif getattr(value, "__parameters__", ()):
            value = value[tuple(args_by_param[param] for param in value.__parameters__)]
    except (NameError, KeyError, TypeError, ValueError):
        # E.g. its value uses names that are not defined yet, or it has a TypeVarTuple that takes several args
        return _UNRESOLVED
    return _TypeAliasValue(alias, args, value)


def _get_annotation_with_metadata(field: FieldInfo) -> Any:
    return _AnnotatedAlias(field.annotation, tuple(field.metadata)) if field.metadata else field.annotation


def _eval_forward_refs(annotation: Any, model: type[BaseModel]) -> Any:
    """Resolve the forward refs in the annotation of a model the way pydantic does, keeping the unresolvable ones"""
    if not _contains_forward_refs(annotation):
        return annotation
    globalns = getattr(sys.modules.get(model.__module__), "__dict__", None)
    localns = {model.__name__: model} | (unpack_lenient_weakvaluedict(model.__pydantic_parent_namespace__) or {})
    evaluated_annotation = _evaluate_user_annotations(lambda: pydantic_try_eval_type(annotation, globalns, localns)[0])
    # The forward refs that are left make discovery keep the head args, like main
    return annotation if evaluated_annotation is _UNRESOLVED else evaluated_annotation


def _contains_forward_refs(annotation: Any) -> bool:
    """Whether the annotation has strings or forward refs where typing evaluates them as types.

    So the strings in the args of Literal and in the metadata of Annotated do not count.
    """
    from typing_inspection.typing_objects import is_annotated, is_literal

    if isinstance(annotation, str | ForwardRef):
        return True
    origin = get_origin(annotation)
    if is_literal(origin):
        return False
    args = get_args(annotation)
    if is_annotated(origin):
        args = args[:1]
    return any(_contains_forward_refs(arg) for arg in args)


def _try_eval_type_for_discovery(value: Any, globals: dict[str, Any]) -> Any:
    """Like _try_eval_type but keeps the string if evaluating it fails in any way"""
    evaluated_value = _evaluate_user_annotations(lambda: _try_eval_type(value, globals))
    return value if evaluated_value is _UNRESOLVED else evaluated_value


def _try_eval_type(value: Any, globals: dict[str, Any]) -> Any:
    new_value, success = pydantic_try_eval_type(value, globals)
    if success:
        return new_value
    else:
        return value
