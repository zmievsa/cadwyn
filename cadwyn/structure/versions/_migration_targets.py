from collections.abc import (
    Callable,  # noqa: TC003 # Needed by runtime annotation introspection.
    Iterable,  # noqa: TC003 # Needed by runtime annotation introspection.
)
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    NamedTuple,
    NoReturn,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel
from typing_extensions import TypeVar
from typing_inspection.typing_objects import is_typealiastype

from cadwyn._utils import (
    UnionType,
    _get_type_alias_value,
    lenient_issubclass,
)
from cadwyn.exceptions import (
    CadwynError,
)

if TYPE_CHECKING:
    from fastapi.routing import APIRoute


def _get_schema_migration_key(annotation: object) -> object:
    """Return the annotation if it can be looked up in schema migration mappings, otherwise None.

    Some annotations (e.g. `list[Annotated[int, {"description": "item"}]]`) are unhashable,
    so no schema migration can ever be registered for them.
    """
    try:
        hash(annotation)
    except TypeError:
        return None
    return annotation


_SchemaInstruction = TypeVar("_SchemaInstruction")


class _SchemaMigrationTarget(NamedTuple):
    """What the schema migrations of a request body or a response model are looked up by."""

    # The annotations whose migrations the body or the response gets as a whole, e.g. the model `A` or `Union[A, B]`.
    # Their migrations replace the ones of the union members, so that none of them runs twice
    keys: tuple[object, ...] = ()
    # The latest versions of the models that are members of the annotation if it's a union, e.g. `Optional[A]`
    union_members: tuple[type[BaseModel], ...] = ()


def _get_schema_migration_target(annotation: object) -> _SchemaMigrationTarget:
    """Return what the schema migrations of a body or a response with this annotation are looked up by.

    Annotated and named type aliases (e.g. `TypeAliasType("Alias", Union[A, B])`) mean the same as their value.
    """
    resolved = _resolve_type_aliases(annotation)
    keys: dict[object, None] = {}
    for candidate in (annotation, resolved):
        if lenient_issubclass(candidate, BaseModel):
            candidate = getattr(candidate, "__cadwyn_original_model__", candidate)
        key = _get_schema_migration_key(candidate)
        if key is not None:
            keys[key] = None
    return _SchemaMigrationTarget(tuple(keys), _get_union_members(resolved))


def _resolve_type_aliases(annotation: object) -> object:
    """Return the annotation without the Annotated and the named type aliases around it."""
    seen_aliases: set[int] = set()
    while True:
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        elif is_typealiastype(annotation) and id(annotation) not in seen_aliases:
            seen_aliases.add(id(annotation))
            annotation = _get_type_alias_value(annotation)
        else:
            return annotation


def _get_union_members(annotation: object) -> tuple[type[BaseModel], ...]:
    """Return the latest versions of the models that a union annotation (e.g. `Optional[Union[A, B]]`) has as members.

    Other annotations, including models in lists, dicts, or generics, have none: migrations don't run for them.
    """
    annotation = _resolve_type_aliases(annotation)
    if not isinstance(annotation, UnionType):
        return ()
    members: dict[type[BaseModel], None] = {}
    for arg in get_args(annotation):
        arg = _resolve_type_aliases(arg)
        if isinstance(arg, UnionType):
            members.update(dict.fromkeys(_get_union_members(arg)))
        elif lenient_issubclass(arg, BaseModel):
            members[getattr(arg, "__cadwyn_original_model__", arg)] = None
    return tuple(members)


def _get_models_of_schema(schema: object) -> tuple[type[BaseModel], ...]:
    """Return the models that a schema of a migration is: the model itself, or the members of a union."""
    if lenient_issubclass(schema, BaseModel):
        return (schema,)
    return _get_union_members(schema)


def _has_union_member_migrations(target: _SchemaMigrationTarget, version_changes: "Iterable[dict[Any, Any]]") -> bool:
    """Whether any of these schema instructions of version changes is for a member of the target's union."""
    return any(member in instructions for instructions in version_changes for member in target.union_members)


def _get_schema_migrations(
    schema_instructions: "dict[Any, list[_SchemaInstruction]]",
    target: _SchemaMigrationTarget,
    get_union_member: "Callable[[], Union[type[BaseModel], None]]",
) -> "list[_SchemaInstruction]":
    """Return the schema migrations of a version change for a body or a response with this target.

    We only look up the member if one of the members has migrations, because that can fail.
    """
    for key in target.keys:
        if key in schema_instructions:
            return schema_instructions[key]
    if any(member in schema_instructions for member in target.union_members):
        member = get_union_member()
        if member is not None and member in schema_instructions:
            return schema_instructions[member]
    return []


def _no_union_member() -> None:
    """Find no union member: for the bodies and responses whose union members have no migrations."""


# Marks that the body param doesn't get the request body as an endpoint argument, so we don't have its validated value
_UNKNOWN_BODY = object()


def _get_closest_union_member(
    value: BaseModel, members: tuple[type[BaseModel], ...], annotation: object
) -> Union[type[BaseModel], None]:
    """Return the member of the union that the model object is, or None if it's no member.

    A subclass of a member, such as an internal representation, gets the migrations of that member. A model that is
    a subclass of several members that don't inherit from each other is ambiguous, so we fail instead of guessing.
    """
    value_type = type(value)
    model = getattr(value_type, "__cadwyn_original_model__", value_type)
    matches = [member for member in members if issubclass(model, member)]
    closest = [match for match in matches if all(issubclass(match, other) for other in matches)]
    if len(closest) == 1:
        return closest[0]
    if not matches:
        return None
    raise CadwynError(
        f"Cadwyn can't tell which member of {annotation!r} the {value_type.__name__} object is because it is "
        f"several of them: {[match.__name__ for match in matches]}. So it can't tell which of their schema "
        "migrations to run. Use an object of only one of them, or migrate this endpoint by its path instead."
    )


def _get_union_member_of_body(
    validated_body: object, members: tuple[type[BaseModel], ...], annotation: object
) -> Union[type[BaseModel], None]:
    """Return the member of the union that the body that the request's version has validated is, if any."""
    if not isinstance(validated_body, BaseModel):
        return None
    return _get_closest_union_member(validated_body, members, annotation)


def _raise_unknown_union_member_of_body(route: "APIRoute", body_field_alias: Union[str, None]) -> NoReturn:
    body_annotation = route.body_field.field_info.annotation if route.body_field is not None else None
    raise CadwynError(
        f"Cadwyn can't tell which member of {body_annotation!r} the body of {sorted(route.methods or ())} "
        f'{route.path} is because the endpoint doesn\'t get the body as its "{body_field_alias}" argument, e.g. '
        "because the body param has an alias. So it can't tell which of their schema migrations to run. Give the "
        "body param of the endpoint no alias, or migrate this endpoint by its path instead."
    )


def _get_request_body_target(head_body_field: object, head_route: "APIRoute") -> _SchemaMigrationTarget:
    """Return what the schema migrations of the body of a route with a single body param are looked up by.

    `head_body_field` is the model of the body of the route's version in the latest version. A union keeps the models
    of the route's version, so we look it up by the annotation of the latest version's route instead.
    """
    if head_body_field is None:
        return _SchemaMigrationTarget()
    annotation = head_route.body_field.field_info.annotation if head_route.body_field is not None else head_body_field
    target = _get_schema_migration_target(annotation)
    key = _get_schema_migration_key(head_body_field)
    if key is None or key in target.keys:
        return target
    return target._replace(keys=(key, *target.keys))
