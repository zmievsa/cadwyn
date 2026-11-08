import collections.abc
import typing
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from pydantic._internal._generics import PydanticGenericMetadata

from cadwyn._utils import (
    _get_type_alias_value,
    lenient_issubclass,
)

from ._annotations import (
    _UNRESOLVED,
    _eval_forward_refs,
    _get_value_of_type_alias_with_args,
    _is_type_position_in_generic,
)

if TYPE_CHECKING:
    from ._models import (
        SchemaGenerator,
        _CycleCopier,
    )


def _alias_value_uses_fallbacks(alias: Any, supplied_count: int) -> bool:
    """Find bare validation uses hidden in an alias value, ignoring its supplied type parameters."""
    from typing_inspection.typing_objects import is_typealiastype

    ignored = frozenset(alias.__type_params__[:supplied_count])
    pending = [(_get_type_alias_value(alias), ignored)]
    visited: set[tuple[int, frozenset[typing.TypeVar]]] = set()
    while pending:
        annotation, ignored = pending.pop()
        key = id(annotation), ignored
        if key in visited:
            continue
        visited.add(key)
        if isinstance(annotation, typing.TypeVar):
            if annotation not in ignored:
                return True
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            pending.extend((field.annotation, ignored) for field in annotation.model_fields.values())
        else:
            origin = get_origin(annotation)
            args = get_args(annotation)
            nested = annotation if is_typealiastype(annotation) else origin
            if is_typealiastype(nested):
                params = nested.__type_params__
                nested_ignored = (ignored - frozenset(params)) | frozenset(params[: len(args)])
                pending.append((_get_type_alias_value(nested), nested_ignored))
            pending.extend(
                (arg, ignored)
                for index, arg in enumerate(args)
                if _is_type_position_in_generic(origin, index) and origin is not collections.abc.Callable
            )
    return False


def _get_typevar_uses(model: type[BaseModel]) -> dict[typing.TypeVar, set[type[BaseModel]]]:
    """Find unresolved parameters in validated fields, excluding unused generic parameters."""
    found: dict[typing.TypeVar, set[type[BaseModel]]] = {}
    visited = {model}
    visited_annotations: set[tuple[int, type[BaseModel]]] = set()
    pending = [(field.annotation, model) for field in model.model_fields.values()]
    while pending:
        annotation, owner = pending.pop()
        key = id(annotation), owner
        if key in visited_annotations:
            continue
        visited_annotations.add(key)
        expanded = _get_value_of_type_alias_with_args(annotation)
        if expanded is not _UNRESOLVED:
            pending.append((expanded.value, owner))
            continue
        if isinstance(annotation, typing.TypeVar):
            found.setdefault(annotation, set()).add(owner)
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation not in visited:
                visited.add(annotation)
                pending.extend((field.annotation, annotation) for field in annotation.model_fields.values())
        else:
            origin = get_origin(annotation)
            pending.extend(
                (arg, owner)
                for index, arg in enumerate(get_args(annotation))
                if _is_type_position_in_generic(origin, index) and origin is not collections.abc.Callable
            )
    return found


def _change_version_of_generic_metadata(
    generator: "Union[SchemaGenerator, _CycleCopier]", schema_generator: "SchemaGenerator", model: type[BaseModel]
) -> PydanticGenericMetadata:
    generic_metadata = model.__pydantic_generic_metadata__
    origin = generic_metadata["origin"]
    if not schema_generator._should_version_generic_args(model, generation_stack=[]):
        if origin is not None and schema_generator._is_copied_with_its_cycle(origin):
            # Pydantic identifies a parametrized generic by its origin and args, so with the head origin, the copy
            # of e.g. TreeNode[int] would be mistaken for the head TreeNode[int] wherever both of them are used
            return {**generic_metadata, "origin": generator.change_version_of_base(origin)}
        return generic_metadata
    # Pydantic resolves the fields of a parametrized generic (such as Page[Item]) through its origin and args
    # so they must point to this version's classes too
    return {
        "origin": generator.change_version_of_base(origin) if origin is not None else None,
        "args": generator.annotation_transformer.change_version_of_annotation(_eval_generic_args(model)),
        "parameters": generic_metadata["parameters"],
    }


def _is_parametrization(model: type[BaseModel]) -> bool:
    """Whether the model is the parametrization of a generic, e.g. `Page[Item]` but not `Page` or `ItemPage`"""
    return model.__pydantic_generic_metadata__["origin"] is not None


def _is_generic_model(model: type[BaseModel]) -> bool:
    """Whether the model is generic (e.g. `Parent[T]`) or parametrized (e.g. `Parent[int]`)"""
    generic_metadata = model.__pydantic_generic_metadata__
    return generic_metadata["origin"] is not None or bool(generic_metadata["parameters"])


def _is_same_parametrization(model: type[Union[BaseModel, Enum]], generic: type[BaseModel]) -> bool:
    """Whether the model is the generic or a parametrization of the same origin with the same args.

    E.g. Page[Category] and Page["Category"] are different classes but they mean the same thing
    """
    if model is generic:
        return True
    origin = generic.__pydantic_generic_metadata__["origin"]
    return (
        lenient_issubclass(model, BaseModel)
        and model.__pydantic_generic_metadata__["origin"] is origin
        and _eval_generic_args(model) == _eval_generic_args(generic)
    )


def _eval_generic_args(model: type[BaseModel]) -> tuple[Any, ...]:
    # Generic arguments such as "Item" in Page["Item"] stay strings in pydantic's generic metadata
    return tuple(_eval_forward_refs(arg, model) for arg in model.__pydantic_generic_metadata__["args"])
