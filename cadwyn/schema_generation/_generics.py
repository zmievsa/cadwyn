import collections.abc
import typing
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    cast,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from pydantic._internal._generics import PydanticGenericMetadata, replace_types

from cadwyn._utils import (
    _get_type_alias_value,
    lenient_issubclass,
)

from ._annotation_evaluation import (
    _eval_forward_refs,
    _get_extra_fields_annotation,
    _is_type_position_in_generic,
)

if TYPE_CHECKING:
    from ._models import (
        _PydanticModelWrapper,
    )
    from ._references import (
        _CycleCopier,
    )
    from ._schema_generator import (
        SchemaGenerator,
    )


def _get_typevar_uses(
    annotation: Any,
    ignored: frozenset[Any] = frozenset(),
    *,
    extra_uses: Union[set[typing.TypeVar], None] = None,
) -> dict[typing.TypeVar, dict[type[BaseModel], None]]:
    """Find the TypeVars that validate values as fallbacks in the annotation, with the models whose fields use them.

    `ignored` holds the parameters of a type alias that its args replace. The parameters of nested aliases are
    ignored the same way, and the TypeVars in the fields of nested models, including their typed extra fields
    (`__pydantic_extra__`), are recorded with those models. `extra_uses` gets the TypeVars of typed extra fields.
    """
    from typing_inspection.typing_objects import is_typealiastype

    # The models that use each TypeVar, in the order of their fields
    found: dict[typing.TypeVar, dict[type[BaseModel], None]] = {}
    # The annotations with their models and ignored parameters, and whether they type extra fields
    pending: list[tuple[Any, Any, frozenset[Any], bool]] = [(annotation, None, ignored, False)]
    # Keeps the annotations alive so that their ids stay unique
    visited: dict[tuple[int, int, frozenset[Any], bool], Any] = {}
    visited_aliases: dict[tuple[int, int, frozenset[Any]], Any] = {}
    while pending:
        annotation, owner, ignored, is_extra = pending.pop()
        key = id(annotation), id(owner), ignored, is_extra
        if key in visited:
            continue
        visited[key] = annotation
        if isinstance(annotation, typing.TypeVar):
            if annotation not in ignored:
                found.setdefault(annotation, {})[owner] = None
                if is_extra and extra_uses is not None:
                    extra_uses.add(annotation)
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested = [(field.annotation, annotation, frozenset(), False) for field in annotation.model_fields.values()]
            if annotation.model_config.get("extra") == "allow":
                nested.append((_get_extra_fields_annotation(annotation), annotation, frozenset(), True))
            # Reversed so that the models come out in the order of the fields
            pending.extend(reversed(nested))
        else:
            origin = get_origin(annotation)
            args = get_args(annotation)
            alias = annotation if is_typealiastype(annotation) else origin
            if is_typealiastype(alias):
                params = alias.__type_params__
                alias_ignored = (ignored - frozenset(params)) | frozenset(params[: len(args)])
                # Each read of the value of a native type alias can return a new object
                alias_key = id(alias), id(owner), alias_ignored
                if alias_key not in visited_aliases:
                    visited_aliases[alias_key] = alias
                    pending.append((_get_type_alias_value(alias), owner, alias_ignored, is_extra))
            # The args of a type alias replace its parameters in its value
            pending.extend(
                reversed(
                    [
                        (arg, owner, ignored, is_extra)
                        for index, arg in enumerate(args)
                        if alias is origin
                        or (_is_type_position_in_generic(origin, index) and origin is not collections.abc.Callable)
                    ]
                )
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


def _substitute_type_vars_of_inherited_annotation(
    model: "_PydanticModelWrapper[BaseModel]",
    schemas: "dict[type[BaseModel], _PydanticModelWrapper[BaseModel]]",
    field_name: str,
    annotation: Any,
) -> Any:
    """Substitute the type vars in the annotation of a field that the model inherits, as pydantic does.

    Pydantic substitutes the type vars of the inherited fields of the parametrizations of generics (e.g. `second: U`
    of `Pair` in `Pair[str, int]` and in `class StrIntPair(Pair[str, int])`) but not of the fields that a class
    defines itself. So when the copy of the model defines such a field because of an instruction, it gets the
    annotation of its parent with the type vars that the parametrizations between them substitute.
    """
    parent = next(parent for parent in model._get_parents(schemas) if field_name in parent.annotations).cls
    # Like pydantic, each class inherits the field from its first base that has it. The other bases, e.g. `A[int]`
    # in `class M(A[int], B[T])`, do not substitute its type vars even if they reuse them.
    classes_on_the_way: list[type] = []
    cls: type = model.cls
    while cls is not parent:
        classes_on_the_way.append(cls)
        cls = next(base for base in cls.__bases__ if lenient_issubclass(base, parent))
    # From the parametrization closest to the parent to the model itself, as pydantic substitutes them
    for cls in reversed(classes_on_the_way):
        origin = getattr(cls, "__pydantic_generic_metadata__", {}).get("origin")
        if origin is not None:
            parameters = origin.__pydantic_generic_metadata__["parameters"]
            type_vars = dict(zip(parameters, _eval_generic_args(cast("type[BaseModel]", cls)), strict=True))
            substituted_annotation = replace_types(annotation, type_vars)
            # Pydantic rebuilds Annotated even without type vars in it, which would give it another cache key
            if substituted_annotation != annotation:
                annotation = substituted_annotation
    return annotation


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
