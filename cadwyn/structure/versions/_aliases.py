import dataclasses
import functools
import itertools
import threading
from collections import defaultdict
from collections.abc import (
    Callable,
    Iterator,
)
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    TypeAlias,
    Union,
    cast,
    get_args,
    get_origin,
)

from pydantic import AliasPath, BaseModel, RootModel
from pydantic.fields import FieldInfo
from pydantic_core import (
    PydanticUndefined,
)

from cadwyn._utils import (
    UnionType,
    lenient_issubclass,
)

from ._core_schema import (
    _CORE_SCHEMA_DEFINITIONS,
    _CORE_SCHEMA_ERRORS,
    _do_compiled_aliases_match,
    _get_model_inner_core_schema,
    _get_validation_paths,
    _get_validation_settings,
    _is_field_schema_safe,
    _is_renamed_key_check,
    _register_core_schema_definitions,
)
from ._migration_targets import _get_schema_migration_key

if TYPE_CHECKING:
    from fastapi._compat import ModelField

# Whether the last restoration renamed a key without which the data surely fails validation
_RESTORATION = threading.local()


def _rescue_body(body_field: "ModelField", data: Any) -> Any:
    """Restore the validation aliases of a `model_dump(by_alias=True)` body only if it surely fails validation as is.

    The dump uses serialization aliases, which can differ from what a field validates
    (e.g. `Field(alias="x", validation_alias="v")`), so the model would silently ignore such values.
    That makes the body surely fail when a required field, or a model that forbids extra keys, gets a dumped key
    instead of a validation key. Bodies that the field accepts as they are stay exactly as they are, even if a field
    gets its default instead of its value. Anything that the restoration plans of the models don't cover is left as is.
    """
    annotation = body_field.field_info.annotation
    # Most bodies have no fields without which they fail, so they aren't walked at all
    if not _annotation_reaches_models_with(annotation, _get_failing_renames, all_fields=False):
        return data
    spec = _get_restorer_spec(annotation)
    if spec is None or not _is_body_field_safe(body_field, spec):
        return data
    return _restore_surely_failing_data(spec, data)


def _rescue_model_data(model: type[BaseModel], data: Any) -> Any:
    """Restore the validation aliases of `model_dump(by_alias=True)` data only if `model.model_validate` surely fails.

    It's what `_rescue_body` does for a body field, for data that the model itself validates.
    """
    # Like `_rescue_body`, we go by the failing keys of `_get_failing_keys`, which come from the field infos even for
    # fields that the core schemas of their types give defaults to: such a field wouldn't fail without its value,
    # but it would silently get its default instead of the value in the body
    if not _annotation_reaches_models_with(model, _get_failing_renames, all_fields=False):
        return data
    return _restore_surely_failing_data(("model", model), data)


def _restore_surely_failing_data(spec: "_RestorerSpec", data: Any) -> Any:
    """Restore the data only if it surely fails validation as is, so that we decide what to validate before validating.

    Validators can have side effects, so the data is validated only once, whichever data it is.
    """
    _RESTORATION.has_failing_renames = False
    restored = _restore_data(spec, data)
    return restored if _RESTORATION.has_failing_renames else data


def _is_dumped_from_models(content: Any, spec: "_RestorerSpec") -> bool:
    """Whether the content holds models of the spec wherever the spec expects a model, and holds at least one.

    Subclasses only count if they dump the fields of the model like the model does. Other values, such as None,
    mean that whatever is in the migrated data didn't come from the dump of a model. We don't look into the models:
    their fields hold their own models.
    """
    stack: list[tuple[Any, _RestorerSpec]] = [(content, spec)]
    has_models = False
    while stack:
        content, (kind, inner) = stack.pop()
        if kind == "model":
            if isinstance(content, BaseModel):
                if not _dumps_like(type(content), inner):
                    return False
                has_models = True
            elif issubclass(inner, RootModel):
                root_spec = _get_restorer_spec(inner.model_fields["root"].annotation)
                if root_spec is None:
                    return False
                stack.append((content, root_spec))
            elif isinstance(content, dict):
                return False
        elif isinstance(content, BaseModel):
            return False
        elif (kind == "list" and isinstance(content, list)) or (kind == "dict" and isinstance(content, dict)):
            items = content if kind == "list" else content.values()
            # Most containers only hold the models themselves, so we check their types in bulk
            item_types = set(map(type, items))
            if inner[0] == "model" and all(_dumps_like(item_type, inner[1]) for item_type in item_types):
                has_models = has_models or bool(item_types)
            else:
                stack.extend(zip(items, itertools.repeat(inner)))
    return has_models


@functools.cache
def _dumps_like(cls: type, model: type[BaseModel]) -> bool:
    """Whether `model_dump()` of `cls` has the keys and values that a dump of `model` has, for the fields of `model`.

    Subclasses can change how fields are dumped, e.g. with serializers or a `model_dump()` override, and add keys that
    the model reads. We only compare by identity and strings, so we never call the `__eq__` of user values such as
    the defaults of fields.
    """
    if cls is model:
        return True
    if (
        not issubclass(cls, model)
        or issubclass(model, RootModel)
        or _get_model_inner_core_schema(cls) is None
        or _get_validation_settings(cls)[2] == "allow"
    ):
        return False
    # The keys that the fields of the model are dumped to or validated from
    model_keys: set[Union[str, int]] = {cf.alias or name for name, cf in model.model_computed_fields.items()}
    for name, field in model.model_fields.items():
        model_keys.add(field.serialization_alias or name)
        model_keys.update(path[0] for path in _get_validation_paths(model, name, field))
    for name, field in cls.model_fields.items():
        model_field = model.model_fields.get(name)
        if model_field is None:
            if (field.serialization_alias or name) in model_keys:
                return False
        elif not _is_field_dumped_like(cls, field, model, model_field, name):
            return False
    for name, computed_field_info in cls.model_computed_fields.items():
        key = computed_field_info.alias or name
        model_computed_field_info = model.model_computed_fields.get(name)
        if key in model_keys and (
            model_computed_field_info is None or (model_computed_field_info.alias or name) != key
        ):
            return False
    return True


def _is_field_dumped_like(
    cls: type[BaseModel], field: FieldInfo, model: type[BaseModel], model_field: FieldInfo, name: str
) -> bool:
    """Whether the field of the subclass `cls` is dumped like the field of `model`, judging by what the dump uses.

    Its type (including the metadata of `Annotated`, such as `PlainSerializer`), its serialization alias, and its
    field serializers. Validators and defaults don't matter because the dumped value has already been validated.
    """
    cls_serializers = _get_field_serializers(cls, name)
    model_serializers = _get_field_serializers(model, name)
    return (
        field.annotation is model_field.annotation
        and field.serialization_alias == model_field.serialization_alias
        and len(field.metadata) == len(model_field.metadata)
        and all(item is model_item for item, model_item in zip(field.metadata, model_field.metadata, strict=True))
        and len(cls_serializers) == len(model_serializers)
        and all(func is model_func for func, model_func in zip(cls_serializers, model_serializers, strict=True))
    )


def _get_field_serializers(model: type[BaseModel], name: str) -> list[Callable[..., Any]]:
    """Return the functions of the field serializers of the field in the order of their names"""
    decorators = model.__pydantic_decorators__.field_serializers
    return [
        decorators[var_name].func
        for var_name in sorted(decorators)
        if name in decorators[var_name].info.fields or "*" in decorators[var_name].info.fields
    ]


_IS_BODY_FIELD_SAFE: "dict[int, tuple[ModelField, bool]]" = {}


def _is_body_field_safe(body_field: "ModelField", spec: "_RestorerSpec") -> bool:
    """Whether the validators of the body parameter (or response field) itself leave the dumped data as valid input.

    They come from the parameter's annotation, such as `Annotated[Model, BeforeValidator(...)]`, and wrap the model
    (and the lists and dicts around it, which `spec` describes) in the core schema that FastAPI validates the body with.
    Its serializers don't matter because we dump the model.
    """
    cached = _IS_BODY_FIELD_SAFE.get(id(body_field))
    if cached is not None and cached[0] is body_field:
        return cached[1]
    try:
        schema = _register_core_schema_definitions(cast("dict[str, Any]", body_field._type_adapter.core_schema))
        while True:
            # `OnErrorOmit` and other defaults that swallow errors make invalid data pass, so we stop at them
            while (
                schema["type"] in ("definition-ref", "nullable", "default", "function-after")
                or _is_renamed_key_check(schema)
            ) and schema.get("on_error", "raise") == "raise":
                schema = (
                    _CORE_SCHEMA_DEFINITIONS[schema["schema_ref"]]
                    if schema["type"] == "definition-ref"
                    else schema["schema"]
                )
            kind, inner = spec
            # The model's own validators and serializers are covered by its restoration plan,
            # and the keys of dicts are never renamed
            if kind == "model" or schema["type"] != kind:
                is_safe = kind == schema["type"] == "model"
                break
            schema = schema["items_schema" if kind == "list" else "values_schema"]
            spec = inner
    except _CORE_SCHEMA_ERRORS:  # pragma: no cover # FastAPI and Pydantic don't build such schemas
        is_safe = False
    _IS_BODY_FIELD_SAFE[id(body_field)] = (body_field, is_safe)
    return is_safe


# What the restoration does with a value: `("list", item_spec)`, `("dict", value_spec)`, or `("model", model)`
_RestorerSpec: TypeAlias = tuple[str, Any]
# Marks the root of a root model, which replaces the model's data instead of a key in it
_ROOT_KEY = object()


def _get_restorer_spec(annotation: object) -> Union[_RestorerSpec, None]:
    """Return what to restore in the data of `annotation`, or None if there's nothing to restore"""
    node = _get_restorable_node(annotation)
    if node is None:
        return None
    kind, inner = node
    if kind == "model":
        return node
    item_spec = _get_restorer_spec(inner)
    return None if item_spec is None else (kind, item_spec)


def _needs_restoring(spec: _RestorerSpec, data: Any, *, look_ahead: bool = True) -> bool:
    kind, inner = spec
    if kind == "list":
        return isinstance(data, list) and bool(data)
    if kind == "dict":
        return isinstance(data, dict) and bool(data)
    plan = _get_restore_plan(inner)
    if plan.root_spec is not None:
        return _needs_restoring(plan.root_spec, data, look_ahead=look_ahead)
    # Most bulk items have none of the keys, so they only cost this check
    return (
        isinstance(data, dict)
        and not data.keys().isdisjoint(plan.keys)
        and _model_needs_restoring(plan, data, look_ahead=look_ahead)
    )


def _model_needs_restoring(plan: "_RestorePlan", data: dict[str, Any], *, look_ahead: bool) -> bool:
    """Whether a rename applies to the data of the model, or one of its nested values might need restoring.

    We look at most one level deep, so that checking deep bodies stays cheap and never recurses deeply.
    """
    # Plain loops instead of `any()` because this runs for every bulk item that has one of the plan's keys
    for rename in plan.renames:
        if _does_rename_apply(rename, data):
            return True
    for key, child_spec in plan.nested:
        if key in data and (not look_ahead or _needs_restoring(child_spec, data[key], look_ahead=False)):
            return True
    return False


def _does_rename_apply(rename: "_Rename", data: dict[str, Any]) -> bool:
    dumped_key, _, validation_paths, validation_keys = rename
    if dumped_key not in data:
        return False
    # The head already finds a value for the field if a migration has set one of its validation paths
    if validation_keys is not None:
        return data.keys().isdisjoint(validation_keys)
    return all(path.search_dict_for_path(data) is PydanticUndefined for path in validation_paths)


def _iter_children_to_restore(spec: _RestorerSpec, data: Any) -> Iterator[tuple[Any, _RestorerSpec, Any]]:
    kind, inner = spec
    if kind == "model":
        plan = _get_restore_plan(inner)
        if plan.root_spec is not None:
            return iter([(_ROOT_KEY, plan.root_spec, data)])
        return (
            (key, child_spec, data[key])
            for key, child_spec in plan.nested
            if key in data and _needs_restoring(child_spec, data[key])
        )
    items = enumerate(data) if kind == "list" else data.items()
    if inner[0] == "model" and (item_plan := _get_restore_plan(inner[1])).root_spec is None:
        # Most bulk items have none of the keys, so they only cost this check
        keys = item_plan.keys
        return (
            (key, inner, item)
            for key, item in items
            if isinstance(item, dict)
            and not item.keys().isdisjoint(keys)
            and _model_needs_restoring(item_plan, item, look_ahead=True)
        )
    return ((key, inner, item) for key, item in items if _needs_restoring(inner, item))


def _restore_data(spec: _RestorerSpec, data: Any) -> Any:
    """Restore the data without recursion, so that bodies as deep as pydantic accepts never hit the recursion limit.

    We only copy the containers in which something changes.
    """
    if not _needs_restoring(spec, data):
        return data
    if _get_spec_height(spec) is not None:
        return _restore_shallow_data(spec, data, is_validated=True)
    # Each frame is [spec, data, children to restore, restored copy of data or None, key of the child being restored,
    # whether the head validates the data of the frame]. It doesn't validate what's under a dumped key.
    stack: list[list[Any]] = [[spec, data, _iter_children_to_restore(spec, data), None, None, True]]
    while True:
        frame = stack[-1]
        child = next(frame[2], None)
        if child is not None:
            key, child_spec, child_data = child
            is_child_validated = frame[5] and (
                frame[0][0] != "model" or _is_nested_key_validated(_get_restore_plan(frame[0][1]), key, frame[1])
            )
            if _get_spec_height(child_spec) is None:
                frame[4] = key
                stack.append(
                    [
                        child_spec,
                        child_data,
                        _iter_children_to_restore(child_spec, child_data),
                        None,
                        None,
                        is_child_validated,
                    ]
                )
            else:
                restored_child = _restore_shallow_data(child_spec, child_data, is_validated=is_child_validated)
                _set_restored_child(frame, key, child_data, restored_child)
            continue
        stack.pop()
        restored_data = _finish_restoring(frame)
        if not stack:
            return restored_data
        _set_restored_child(stack[-1], stack[-1][4], frame[1], restored_data)


def _set_restored_child(parent: list[Any], key: Any, child_data: Any, restored_child: Any) -> None:
    if restored_child is child_data:
        return
    if key is _ROOT_KEY:
        parent[3] = restored_child
        return
    if parent[3] is None:
        parent[3] = list(parent[1]) if isinstance(parent[1], list) else dict(parent[1])
    parent[3][key] = restored_child


# The greatest number of nested models, lists and dicts that we restore with recursion.
# Each of them costs a frame or two, so restoring with recursion stays far from the recursion limit.
_MAX_SHALLOW_HEIGHT = 8
# The number of nested models, lists and dicts in the data of each model, or None if it can be deeper than the maximum
_SHALLOW_HEIGHTS: dict[type[BaseModel], Union[int, None]] = {}


def _get_spec_height(spec: _RestorerSpec) -> Union[int, None]:
    """Return how many models, lists and dicts restoring the data of the spec walks into at most.

    It's None if it's more than we restore with recursion, e.g. for models that refer to themselves.
    """
    containers, model = _split_spec(spec)
    height = _get_model_height(model)
    return None if height is None or containers + height > _MAX_SHALLOW_HEIGHT else containers + height


def _split_spec(spec: _RestorerSpec) -> tuple[int, type[BaseModel]]:
    """Return the number of lists and dicts around the model of the spec, and the model"""
    containers = 0
    while spec[0] != "model":
        containers += 1
        spec = spec[1]
    return containers, spec[1]


def _get_model_height(root: type[BaseModel]) -> Union[int, None]:
    """Return the height of the data of the model, computed without recursion so that it never hits the recursion limit.

    We only cache final heights, so other threads never see unfinished ones.
    """
    if root in _SHALLOW_HEIGHTS:
        return _SHALLOW_HEIGHTS[root]
    # Each frame is [model, the (containers, model) of its nested fields, its height so far, containers of the child]
    stack: list[list[Any]] = [[root, _iter_nested_models(root), 1, 0]]
    models_on_stack = {root}
    child_height: Union[int, None] = 0
    while True:
        frame = stack[-1]
        child = next(frame[1], None)
        if child is not None:
            containers, model = child
            if model in _SHALLOW_HEIGHTS:
                child_height = _SHALLOW_HEIGHTS[model]
            # A model that is being computed refers to itself through the frame's model
            elif model in models_on_stack:
                child_height = None
            else:
                frame[3] = containers
                stack.append([model, _iter_nested_models(model), 1, 0])
                models_on_stack.add(model)
                continue
        else:
            stack.pop()
            models_on_stack.discard(frame[0])
            child_height = frame[2]
            _SHALLOW_HEIGHTS[frame[0]] = child_height
            if not stack:
                return child_height
            frame = stack[-1]
            containers = frame[3]
        if child_height is None or containers + child_height + 1 > _MAX_SHALLOW_HEIGHT:
            # The model is as deep as its child, so we finish it right away
            frame[1] = iter(())
            frame[2] = None
        else:
            frame[2] = max(frame[2], containers + child_height + 1)


def _iter_nested_models(model: type[BaseModel]) -> Iterator[tuple[int, type[BaseModel]]]:
    plan = _get_restore_plan(model)
    # Root models are always walked
    if plan.root_spec is not None:
        return iter([(0, model)])
    return (_split_spec(child_spec) for _, child_spec in plan.nested)


def _restore_shallow_data(spec: _RestorerSpec, data: Any, *, is_validated: bool) -> Any:
    """Restore the data of a spec that has a height with recursion, which avoids the frames of the walk.

    The result is the same as the walk's: the walk only skips the data that wouldn't change.
    """
    kind, inner = spec
    if kind == "model":
        plan = _get_restore_plan(inner)
        if not isinstance(data, dict) or data.keys().isdisjoint(plan.keys):
            return data
        return _restore_shallow_model_data(plan, data, is_validated=is_validated)
    if kind == "list" and isinstance(data, list):
        items = enumerate(data)
    elif kind == "dict" and isinstance(data, dict):
        items = data.items()
    else:
        return data
    restored = None
    plan = _get_restore_plan(inner[1]) if inner[0] == "model" else None
    for key, item in items:
        if plan is None:
            restored_item = _restore_shallow_data(inner, item, is_validated=is_validated)
        elif not isinstance(item, dict):
            continue
        elif not plan.nested:
            # Most bodies are lists of models without nested models to restore, so their items only cost the renames
            restored_item = _rename_keys(plan, item, is_validated=is_validated)
        # Most bulk items have none of the keys, so they only cost this check
        elif item.keys().isdisjoint(plan.keys):
            continue
        else:
            restored_item = _restore_shallow_model_data(plan, item, is_validated=is_validated)
        if restored_item is not item:
            if restored is None:
                restored = list(data) if kind == "list" else dict(data)
            restored[key] = restored_item
    return data if restored is None else restored


def _restore_shallow_model_data(plan: "_RestorePlan", data: dict[str, Any], *, is_validated: bool) -> Any:
    restored = None
    for key, child_spec in plan.nested:
        if key in data:
            child = data[key]
            # The head doesn't validate what's under a dumped key, or under a key that another key of its field shadows
            restored_child = _restore_shallow_data(
                child_spec, child, is_validated=is_validated and _is_nested_key_validated(plan, key, data)
            )
            if restored_child is not child:
                if restored is None:
                    restored = dict(data)
                restored[key] = restored_child
    return _rename_keys(plan, data, restored, is_validated=is_validated)


def _finish_restoring(frame: list[Any]) -> Any:
    (kind, inner), data, _, restored, _, is_validated = frame
    if kind == "model" and (plan := _get_restore_plan(inner)).root_spec is None:
        return _rename_keys(plan, data, restored, is_validated=is_validated)
    return data if restored is None else restored


def _rename_keys(plan: "_RestorePlan", data: dict[str, Any], restored: Any = None, *, is_validated: bool) -> Any:
    """Apply the renames of the plan to the data of a model, or to its restored copy if there is one.

    Only the renames in data that the head validates can make it surely fail.
    """
    for rename in plan.renames:
        # This runs for every bulk item, so it inlines the common case of `_does_rename_apply`
        dumped_key, target_key, _, validation_keys = rename
        if dumped_key in data and (
            data.keys().isdisjoint(validation_keys) if validation_keys is not None else _does_rename_apply(rename, data)
        ):
            if restored is None:
                restored = dict(data)
            restored[target_key] = restored.pop(dumped_key)
            if is_validated and dumped_key in plan.failing_keys:
                _RESTORATION.has_failing_renames = True
    return data if restored is None else restored


def _get_restorable_node(annotation: object) -> Union[tuple[str, Any], None]:
    """Return what the restoration does with the data of `annotation`: walk into a list, a dict, or a model.

    It's the only place that decides it, so planning and restoration can't disagree.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin, args = get_origin(annotation), get_args(annotation)
    if isinstance(annotation, UnionType):
        options = [arg for arg in args if arg is not type(None)]
        # With several options, we can't know which one is going to validate the data so we leave it as is
        return _get_restorable_node(options[0]) if len(options) == 1 else None
    if origin is list and len(args) == 1:
        return "list", args[0]
    if origin is dict and len(args) == 2:
        return "dict", args[1]
    if lenient_issubclass(annotation, BaseModel):
        return "model", annotation
    return None


# Marks computed fields among the owners of dumped keys, apart from the regular fields
_COMPUTED_FIELD = object()
# (dumped_key, target_key, validation_paths, validation_keys): the keys are None unless all paths are plain keys
_Rename: TypeAlias = tuple[str, str, tuple[AliasPath, ...], Union[frozenset[str], None]]


@dataclasses.dataclass(frozen=True)
class _RestorePlan:
    # The fields to rename
    renames: tuple["_Rename", ...] = ()
    # (dumped_key, spec) of the fields to walk into
    nested: tuple[tuple[str, _RestorerSpec], ...] = ()
    # What to restore in the root of root models
    root_spec: Union[_RestorerSpec, None] = None
    # The dumped keys of `renames` and `nested`
    keys: frozenset[str] = frozenset()
    # The dumped keys of `renames` without which the data of the model surely fails validation:
    # the keys of required fields, or all of them if the model forbids extra keys
    failing_keys: frozenset[str] = frozenset()
    # The dumped keys of `renames`
    renamed_keys: frozenset[str] = frozenset()
    # The other keys that the fields of `nested` validate, by their dumped keys. The model ignores the dumped key of
    # such a field if the data has one of them, e.g. a key of `AliasChoices` that comes before it.
    other_validation_keys: dict[str, frozenset[Union[str, int]]] = dataclasses.field(
        default_factory=dict, compare=False
    )


@functools.cache
def _get_restore_plan(model: type[BaseModel]) -> _RestorePlan:
    # Most apps have no fields to rename, so they don't need to pay for reading core schemas at all
    if not _reaches_models_with(model, _get_rename_candidates, all_fields=True):
        return _RestorePlan()
    renames, traversable = _get_renames_and_traversable_fields(model)
    nested = tuple(
        (key, spec)
        for key, annotation in traversable
        if (spec := _get_restorer_spec(annotation)) is not None
        and _annotation_reaches_models_with(annotation, _get_renames, all_fields=False)
    )
    if issubclass(model, RootModel):
        return _RestorePlan(root_spec=nested[0][1] if nested else None)
    keys = frozenset(key for key, *_ in (*renames, *nested))
    return _RestorePlan(
        renames=renames,
        nested=nested,
        keys=keys,
        failing_keys=_get_failing_keys(model, renames),
        renamed_keys=frozenset(key for key, *_ in renames),
        other_validation_keys=_get_other_validation_keys(model, frozenset(key for key, _ in nested)),
    )


def _get_other_validation_keys(
    model: type[BaseModel], nested_keys: frozenset[str]
) -> dict[str, frozenset[Union[str, int]]]:
    other_validation_keys: dict[str, frozenset[Union[str, int]]] = {}
    for name, field in model.model_fields.items():
        dumped_key = field.serialization_alias or name
        if dumped_key in nested_keys:
            keys = frozenset(path[0] for path in _get_validation_paths(model, name, field) if path[0] != dumped_key)
            if keys:
                other_validation_keys[dumped_key] = keys
    return other_validation_keys


def _is_nested_key_validated(plan: _RestorePlan, key: object, data: dict[str, Any]) -> bool:
    """Whether the model of the plan validates what's under the key of its data, the dumped key of one of its fields.

    It doesn't if it's the key of a field that we rename, or if the data has another key of the field, which might
    come first.
    """
    if key in plan.renamed_keys:
        return False
    other_keys = plan.other_validation_keys.get(cast("str", key))
    return other_keys is None or data.keys().isdisjoint(other_keys)


def _get_failing_keys(model: type[BaseModel], renames: tuple["_Rename", ...]) -> frozenset[str]:
    if _get_validation_settings(model)[2] == "forbid":
        return frozenset(dumped_key for dumped_key, *_ in renames)
    # We go by the field infos even for fields that the core schemas of their types give defaults to: such a field
    # wouldn't fail without its value, but it would silently get its default instead of the client's value
    required_dumped_keys = {
        field.serialization_alias or name for name, field in model.model_fields.items() if field.is_required()
    }
    return frozenset(dumped_key for dumped_key, *_ in renames if dumped_key in required_dumped_keys)


def _get_failing_renames(model: type[BaseModel]) -> tuple[object, ...]:
    return tuple(_get_restore_plan(model).failing_keys)


def _annotation_reaches_models_with(
    annotation: object, get_items: Callable[[type[BaseModel]], tuple[object, ...]], *, all_fields: bool
) -> bool:
    """Whether walking into the data of `annotation` reaches a model for which `get_items` returns anything"""
    return any(
        _reaches_models_with(model, get_items, all_fields=all_fields) for model in _get_restorable_models(annotation)
    )


_RESTORABLE_MODELS: dict[object, tuple[type[BaseModel], ...]] = {}


def _get_restorable_models(annotation: object) -> tuple[type[BaseModel], ...]:
    # Unhashable annotations (e.g. with dicts in `Annotated`) are rare, so we don't cache them
    cache_key = _get_schema_migration_key(annotation)
    if cache_key is None:
        return tuple(_iter_restorable_models(annotation))
    if cache_key not in _RESTORABLE_MODELS:
        _RESTORABLE_MODELS[cache_key] = tuple(_iter_restorable_models(annotation))
    return _RESTORABLE_MODELS[cache_key]


def _iter_restorable_models(annotation: object) -> Iterator[type[BaseModel]]:
    """Yield the models that the restoration walks into when walking into the data of `annotation`"""
    node = _get_restorable_node(annotation)
    while node is not None and node[0] != "model":
        node = _get_restorable_node(node[1])
    if node is not None:
        yield node[1]


def _get_renames(model: type[BaseModel]) -> tuple[object, ...]:
    # Models without rename candidates don't need their core schemas read
    return _get_renames_and_traversable_fields(model)[0] if _get_rename_candidates(model) else ()


_REACHES_MODELS_WITH: dict[tuple[Callable[..., object], bool, type[BaseModel]], bool] = {}


def _reaches_models_with(
    root: type[BaseModel], get_items: Callable[[type[BaseModel]], tuple[object, ...]], *, all_fields: bool
) -> bool:
    """Whether walking into the data of `root` reaches a model for which `get_items` returns anything.

    We walk into all the fields of the models if `all_fields`, and into their traversable fields otherwise.
    Each model is explored once: if nothing is found, nothing is reachable from any of the explored models either.
    """
    cache_key = (get_items, all_fields, root)
    if cache_key in _REACHES_MODELS_WITH:
        return _REACHES_MODELS_WITH[cache_key]
    explored: set[type[BaseModel]] = set()
    parents: dict[type[BaseModel], type[BaseModel]] = {}
    to_explore = [root]
    while to_explore:
        model = to_explore.pop()
        if model in explored or _REACHES_MODELS_WITH.get((get_items, all_fields, model)) is False:
            continue
        if _REACHES_MODELS_WITH.get((get_items, all_fields, model)) or get_items(model):
            # Everything on the way from the root reaches it too
            while True:
                _REACHES_MODELS_WITH[get_items, all_fields, model] = True
                if model is root:
                    return True
                model = parents[model]
        explored.add(model)
        if all_fields:
            annotations = [field.annotation for field in model.model_fields.values()]
        elif _reaches_models_with(model, _get_rename_candidates, all_fields=True):
            annotations = [annotation for _, annotation in _get_renames_and_traversable_fields(model)[1]]
        else:
            # Nothing below this model has rename candidates, so there's no need to read its core schema
            annotations = []
        for nested in (nested for annotation in annotations for nested in _get_restorable_models(annotation)):
            parents.setdefault(nested, model)
            to_explore.append(nested)
    for model in explored:
        _REACHES_MODELS_WITH[get_items, all_fields, model] = False
    return False


@functools.cache
def _get_rename_candidates(model: type[BaseModel]) -> tuple[str, ...]:
    """Return the fields whose dumped key the model doesn't validate, judging by their field info only"""
    return tuple(
        name
        for name, field in model.model_fields.items()
        for paths in [_get_validation_paths(model, name, field)]
        if [field.serialization_alias or name] not in paths and any(len(path) == 1 for path in paths)
    )


@functools.cache
def _get_renames_and_traversable_fields(
    model: type[BaseModel],
) -> tuple[tuple[_Rename, ...], tuple[tuple[str, object], ...]]:
    """Return the fields of the model to rename, and the fields whose values we can walk into.

    We only touch a field when the dumped value is unambiguously its valid input. Everything else, such as fields
    that can only be validated through an `AliasPath`, is left as on main.
    """
    inner_schema = _get_model_inner_core_schema(model)
    # A client's extra key can't be told apart from a dumped key
    if inner_schema is None or model.model_config.get("extra") == "allow":
        return (), ()
    if issubclass(model, RootModel):
        if not _is_field_schema_safe({"root": inner_schema}, "root"):
            return (), ()
        return (), (("root", model.model_fields["root"].annotation),)
    field_schemas = inner_schema["fields"]
    # The keys of every field decide which renames are unambiguous, so we only trust the field infos if the compiled
    # schema validates and dumps all fields by the same aliases. `__get_pydantic_core_schema__` can change them.
    if set(field_schemas) != set(model.model_fields) or not all(
        _do_compiled_aliases_match(field, field_schemas[name]) for name, field in model.model_fields.items()
    ):
        return (), ()
    fields = {
        name: (field.serialization_alias or name, _get_validation_paths(model, name, field))
        for name, field in model.model_fields.items()
    }
    dumped_key_owners: dict[str, set[object]] = defaultdict(set)
    validated_key_owners: dict[Union[str, int], set[str]] = defaultdict(set)
    for name, (dumped_key, paths) in fields.items():
        dumped_key_owners[dumped_key].add(name)
        for path in paths:
            validated_key_owners[path[0]].add(name)
    # Computed fields are dumped too, so their keys are ambiguous as well
    for name, computed_field_info in model.model_computed_fields.items():
        dumped_key_owners[computed_field_info.alias or name].add((_COMPUTED_FIELD, name))
    renames = []
    traversable = []
    for name, (dumped_key, paths) in fields.items():
        annotation = model.model_fields[name].annotation
        keys = [path[0] for path in paths if len(path) == 1]
        is_validated = [dumped_key] in paths
        # A key that belongs to several fields is ambiguous, e.g. another field might read into it via `AliasPath`
        if (
            dumped_key_owners[dumped_key] != {name}
            or not validated_key_owners.get(dumped_key, set()) <= {name}
            # Fields that neither need a rename nor lead to one don't need their core schemas read
            or (
                is_validated
                and not _annotation_reaches_models_with(annotation, _get_rename_candidates, all_fields=True)
            )
            or not _is_field_schema_safe(field_schemas, name)
        ):
            continue
        if is_validated:
            traversable.append((dumped_key, annotation))
        elif keys and keys[0] not in dumped_key_owners and validated_key_owners[keys[0]] == {name}:
            target_key = cast("str", keys[0])
            validation_keys = frozenset(cast("str", p[0]) for p in paths) if all(len(p) == 1 for p in paths) else None
            validation_paths = tuple(AliasPath(cast("str", p[0]), *p[1:]) for p in paths)
            renames.append((dumped_key, target_key, validation_paths, validation_keys))
            traversable.append((dumped_key, annotation))
    return tuple(renames), tuple(traversable)
