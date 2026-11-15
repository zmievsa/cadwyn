from collections.abc import (
    Callable,
    Iterator,
    Mapping,
)
from typing import (
    Any,
    TypeAlias,
    Union,
    cast,
)

from pydantic import AliasChoices, AliasPath, BaseModel, RootModel
from pydantic.fields import FieldInfo
from pydantic_core import (
    PydanticCustomError,
)

_UNION_SCHEMA_TYPES = frozenset(("union", "tagged-union"))


def _get_schema_shape(schema: dict[str, Any]) -> object:
    """Return what kind of data an unwrapped core schema validates into, or None if we don't walk into such data.

    Models have the shape of the head's model that they are copies of. We don't walk into tuples, sets, and other
    containers: pydantic validates their data into them, and strict schemas reject lists, which our copies would be.
    """
    schema_type = schema["type"]
    if schema_type == "model":
        return "model", getattr(schema["cls"], "__cadwyn_original_model__", schema["cls"])
    if schema_type in ("list", "dict"):
        return schema_type
    return None


def _has_core_schema_of_types(schema: dict[str, Any], types: frozenset[str]) -> bool:
    """Whether the core schema, its definitions, or the ones it refers to have a schema of one of the types.

    It's found without recursion.
    """
    stack: list[Any] = [schema]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if isinstance(node, (dict, list, tuple)) and id(node) not in seen:
            seen.add(id(node))
            if not isinstance(node, dict):
                stack.extend(node)
            elif node.get("type") in types:
                return True
            elif node.get("type") == "definition-ref" and node.get("schema_ref") in _CORE_SCHEMA_DEFINITIONS:
                stack.append(_CORE_SCHEMA_DEFINITIONS[node["schema_ref"]])
            else:
                # Defaults, literal values, and metadata are data, not schemas
                stack.extend(value for key, value in node.items() if key not in ("default", "expected", "metadata"))
    return False


def _get_unchanging_model_core_schema(model: type[BaseModel]) -> Union[dict[str, Any], None]:
    """Return the fields core schema (or the root core schema) of a model that keeps its values as they are, or None.

    Besides what `_get_model_inner_core_schema` rejects, its validators of any kind and `model_post_init` could
    change the values: they wrap the model schema or its fields schema, or are its "post_init".
    """
    inner_schema = _get_model_inner_core_schema(model)
    if inner_schema is None:
        return None
    schema = _register_core_schema_definitions(cast("dict[str, Any]", model.__pydantic_core_schema__))
    if schema["type"] == "definition-ref":
        schema = _CORE_SCHEMA_DEFINITIONS[schema["schema_ref"]]
    # `_get_model_inner_core_schema` unwraps the "after" validators of the model and of its fields schema, so we check
    # that the model schema is the outermost one and that its fields schema is its own schema
    if schema["type"] != "model" or schema.get("post_init") or schema["schema"] is not inner_schema:
        return None
    return inner_schema


def _get_dumped_key(name: str, field: FieldInfo) -> str:
    # An empty alias is an alias too
    return name if field.serialization_alias is None else field.serialization_alias


# Reading a core schema of an unexpected shape fails with one of these. We leave such models and fields as on main.
_CORE_SCHEMA_ERRORS = (LookupError, TypeError, AttributeError)
# The core schema types whose dumped values are their valid input, and their keys that hold child schemas as
# `(key, holds_several_schemas, is_optional)`. Any other type, such as "json" or the validators that get the raw input
# ("function-before", "function-wrap", "function-plain"), is unsafe.
# Data-valued keys such as defaults, literal values, metadata, or serializers are never walked.
_CHILD_SCHEMA_KEYS: dict[str, tuple[tuple[str, bool, bool], ...]] = {
    **dict.fromkeys(
        (
            *("any", "none", "bool", "int", "float", "decimal", "complex", "str", "bytes", "literal", "enum"),
            *("date", "time", "datetime", "timedelta", "uuid", "url", "multi-host-url", "missing-sentinel"),
            *("is-instance", "is-subclass", "callable"),
        ),
        (),
    ),
    **dict.fromkeys(("list", "set", "frozenset", "generator"), (("items_schema", False, True),)),
    **dict.fromkeys(
        (
            *("nullable", "default", "function-after", "custom-error", "model", "dataclass"),
            *("model-field", "dataclass-field", "typed-dict-field"),
        ),
        (("schema", False, False),),
    ),
    "tuple": (("items_schema", True, False),),
    "dict": (("keys_schema", False, True), ("values_schema", False, True)),
    "lax-or-strict": (("lax_schema", False, False), ("strict_schema", False, False)),
    "json-or-python": (("json_schema", False, False), ("python_schema", False, False)),
    "chain": (("steps", True, False),),
    "union": (("choices", True, False),),
    "tagged-union": (("choices", True, False),),
    "model-fields": (("fields", True, False), ("extras_schema", False, True)),
    "typed-dict": (("fields", True, False), ("extras_schema", False, True)),
    "dataclass-args": (("fields", True, False),),
}
# Parts of core schemas that are summarized separately because many schemas share them: definitions by their ref
# and models by their class. This keeps the reading of core schemas linear in their size.
_CoreSchemaUnit: TypeAlias = tuple[str, object]
_CORE_SCHEMA_DEFINITIONS: dict[str, dict[str, Any]] = {}
_CORE_SCHEMA_MODELS: dict[object, dict[str, Any]] = {}
# Units are kept in the order the schema refers to them, so that the exploration order doesn't depend on hashing
_CORE_SCHEMA_UNIT_SUMMARIES: dict[_CoreSchemaUnit, tuple[bool, tuple[_CoreSchemaUnit, ...]]] = {}
_IS_CORE_SCHEMA_UNIT_SAFE: dict[_CoreSchemaUnit, bool] = {}


def _register_core_schema_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    """Remember the definitions of a "definitions" core schema and return the schema that refers to them"""
    if schema["type"] != "definitions":
        return schema
    for definition in schema["definitions"]:
        _CORE_SCHEMA_DEFINITIONS.setdefault(definition["ref"], definition)
    return schema["schema"]


def _do_compiled_aliases_match(field: FieldInfo, field_schema: object) -> bool:
    """Whether the compiled schema validates and dumps the field by the aliases of its field info.

    A custom `__get_pydantic_core_schema__` can change them, and then we don't know which keys the head reads.
    """
    return (
        isinstance(field_schema, dict)
        and _normalize_validation_alias(field_schema.get("validation_alias"))
        == _normalize_validation_alias(field.validation_alias)
        and field_schema.get("serialization_alias") == field.serialization_alias
    )


def _normalize_validation_alias(alias: object) -> object:
    if isinstance(alias, str):
        return [[alias]]
    if isinstance(alias, (AliasPath, AliasChoices)):
        alias = alias.convert_to_aliases()
    # Compiled `AliasChoices` are lists of paths, while a compiled `AliasPath` is a single path
    if isinstance(alias, list) and alias and not all(isinstance(path, list) for path in alias):
        alias = [alias]
    return alias


def _get_model_inner_core_schema(model: type[BaseModel]) -> Union[dict[str, Any], None]:
    """Return the fields core schema of the model (or the root core schema of a root model).

    Return None instead if a validator of the model gets the raw input, if the model has a model serializer,
    or if its core schema isn't shaped like the one of a plain model.
    """
    try:
        schema = _register_core_schema_definitions(cast("dict[str, Any]", model.__pydantic_core_schema__))
        if schema["type"] == "definition-ref":
            schema = _CORE_SCHEMA_DEFINITIONS[schema["schema_ref"]]
        # "after" model validators wrap the model schema and only get the validated model
        while schema["type"] == "function-after" or _is_renamed_key_check(schema):
            schema = schema["schema"]
        # A custom `__init__` might expect the input that the dump doesn't have
        if (
            schema["type"] != "model"
            or schema["cls"] is not model
            or schema.get("custom_init")
            or _is_custom_serialization(schema.get("serialization"))
            or _overrides_dumping(model)
            or not _do_compiled_settings_match(model, schema.get("config"))
        ):
            return None
        inner_schema = schema["schema"] if schema["root_model"] else _get_fields_core_schema(schema)
        # "wrap" and "plain" model validators wrap the model schema, while "before" ones wrap its fields schema
        if not schema["root_model"] and inner_schema["type"] != "model-fields":
            return None
    except _CORE_SCHEMA_ERRORS:
        return None
    return inner_schema


# The keys that the fields of a generated model lost because their aliases followed `alias`, which
# `_reject_keys_renamed_by_alias` rejects unless a field of the model reads them: `{field_name: renamed_keys}`
_KEYS_RENAMED_BY_ALIAS = "__cadwyn_keys_renamed_by_alias__"
# The checks that each model class makes, which we compute from its compiled core schema the first time it validates
_RENAMED_KEY_CHECKS = "__cadwyn_renamed_key_checks__"


def _reject_keys_renamed_by_alias(cls: type[BaseModel], data: Any, handler: Callable[[Any], Any]) -> Any:
    """Fail instead of silently giving a field its default when the data has the key that an alias instruction renamed.

    For example, `field("x").had(alias="old")` on `x: str = Field("", alias="new")` makes an older version use "old"
    instead of "new". So without migrations that rename the key, its clients that still send "new" and the responses
    that the latest version returns with "new" would lose the value of the field. We only look at the data as it
    comes in: it fails if it has the key that the field lost but none of the keys that the field reads now.
    """
    if isinstance(data, Mapping):
        for name, renamed_keys, keys_read_by_field in _get_renamed_key_checks(cls):
            renamed_key = next((renamed_key for renamed_key in renamed_keys if renamed_key in data), None)
            if renamed_key is not None and not any(key in data for key in keys_read_by_field):
                raise PydanticCustomError(
                    "renamed_key",
                    'The key "{renamed_key}" of "{field}" was renamed to "{key}" in this API version',
                    {"renamed_key": renamed_key, "field": f"{cls.__name__}.{name}", "key": keys_read_by_field[0]},
                )
    return handler(data)


def _get_renamed_key_checks(model: type[BaseModel]) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]:
    """Return `(field_name, renamed_keys, keys_that_the_field_reads)` for the renamed keys that no field reads.

    The keys come from the compiled core schema of the model itself, so subclasses that change their fields and
    the settings that pydantic changes when it compiles them get their own.
    """
    checks = model.__dict__.get(_RENAMED_KEY_CHECKS)
    if checks is None:
        keys_read_by_fields = _get_keys_read_by_fields(model)
        keys_read_by_model = {key for keys in keys_read_by_fields.values() for key in keys}
        checks = tuple(
            (name, tuple(sorted(renamed_keys - keys_read_by_model)), keys_read_by_fields[name])
            for name, renamed_keys in getattr(model, _KEYS_RENAMED_BY_ALIAS).items()
            if name in keys_read_by_fields and not renamed_keys <= keys_read_by_model
        )
        setattr(model, _RENAMED_KEY_CHECKS, checks)
    return checks


def _get_keys_read_by_fields(model: type[BaseModel]) -> dict[str, tuple[str, ...]]:
    """Return the keys of the input that pydantic reads for each field, from the compiled core schema of the model"""
    schema = cast("dict[str, Any]", model.__pydantic_core_schema__)
    definitions: dict[str, dict[str, Any]] = {}
    if schema["type"] == "definitions":
        definitions = {definition["ref"]: definition for definition in schema["definitions"]}
        schema = schema["schema"]
    # Validators of the model, including `_reject_keys_renamed_by_alias`, wrap it
    while schema["type"] != "model":
        schema = definitions[schema["schema_ref"]] if schema["type"] == "definition-ref" else schema["schema"]
    config = schema.get("config", {})
    # Like pydantic-core, which reads the alias if it validates by alias, and the name if it validates by name or if
    # the field has no alias
    validate_by_alias, validate_by_name = config.get("validate_by_alias", True), config.get("validate_by_name", False)
    fields_schema = schema["schema"]
    # "before" and "after" validators of the model wrap its fields schema
    while fields_schema["type"] != "model-fields":
        fields_schema = fields_schema["schema"]
    keys_read_by_fields = {}
    for name, field_schema in fields_schema["fields"].items():
        alias = field_schema.get("validation_alias")
        paths = cast("list[list[str]]", _normalize_validation_alias(alias)) if alias is not None else []
        keys = [path[0] for path in paths] if validate_by_alias else []
        if alias is None or validate_by_name:
            keys.append(name)
        keys_read_by_fields[name] = tuple(keys)
    return keys_read_by_fields


def _is_renamed_key_check(schema: dict[str, Any]) -> bool:
    """Whether the core schema is the check that Cadwyn adds to the models whose alias instructions renamed keys.

    It only rejects some data and never changes any, so it's safe wherever the model that it wraps is.
    """
    return (
        schema["type"] == "function-wrap"
        and getattr(schema["function"]["function"], "__func__", None) is _reject_keys_renamed_by_alias
    )


def _get_fields_core_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the fields core schema of a model or dataclass core schema"""
    fields_schema = schema["schema"]
    # "after" validators (including `root_validator(skip_on_failure=True)`) wrap the fields schema
    while fields_schema["type"] == "function-after":
        fields_schema = fields_schema["schema"]
    return fields_schema


def _overrides_dumping(cls: object) -> bool:
    """Whether a class overrides how pydantic dumps it, which its core schema doesn't describe"""
    return any(
        name in vars(base)
        for base in getattr(cls, "__mro__", ())
        if base not in (BaseModel, RootModel, object)
        for name in ("model_dump", "model_dump_json")
    )


def _is_field_schema_safe(schemas: dict[str, Any], key: str) -> bool:
    """Whether the dumped value of the field with the core schema at `schemas[key]` is its valid input"""
    units: dict[_CoreSchemaUnit, None] = {}
    try:
        if _has_unsafe_core_schema(schemas[key], units):
            return False
    except _CORE_SCHEMA_ERRORS:
        return False
    return all(_is_core_schema_unit_safe(unit) for unit in units)


def _is_core_schema_unit_safe(root: _CoreSchemaUnit) -> bool:
    """Whether nothing reachable from the core schema unit makes its dumped values invalid input.

    Each unit is explored once: if nothing unsafe is found, nothing is reachable from any of the explored units either.
    """
    if root in _IS_CORE_SCHEMA_UNIT_SAFE:
        return _IS_CORE_SCHEMA_UNIT_SAFE[root]
    explored: set[_CoreSchemaUnit] = set()
    parents: dict[_CoreSchemaUnit, _CoreSchemaUnit] = {}
    to_explore = [root]
    while to_explore:
        unit = to_explore.pop()
        is_safe = _IS_CORE_SCHEMA_UNIT_SAFE.get(unit)
        if unit in explored or is_safe:
            continue
        is_unsafe, units = (True, ()) if is_safe is False else _summarize_core_schema_unit(unit)
        if is_unsafe:
            # Everything on the way from the root reaches it too
            while True:
                _IS_CORE_SCHEMA_UNIT_SAFE[unit] = False
                if unit == root:
                    return False
                unit = parents[unit]
        explored.add(unit)
        for child in units:
            parents.setdefault(child, unit)
            to_explore.append(child)
    for unit in explored:
        _IS_CORE_SCHEMA_UNIT_SAFE[unit] = True
    return True


def _summarize_core_schema_unit(unit: _CoreSchemaUnit) -> tuple[bool, tuple[_CoreSchemaUnit, ...]]:
    if unit not in _CORE_SCHEMA_UNIT_SUMMARIES:
        kind, key = unit
        body = (_CORE_SCHEMA_DEFINITIONS if kind == "ref" else _CORE_SCHEMA_MODELS).get(key)
        units: dict[_CoreSchemaUnit, None] = {}
        try:
            # A definition that we haven't seen is unsafe
            is_unsafe = body is None or _has_unsafe_core_schema(body, units, is_unit_body=True)
        except _CORE_SCHEMA_ERRORS:
            is_unsafe = True
        _CORE_SCHEMA_UNIT_SUMMARIES[unit] = (is_unsafe, tuple(units))
    return _CORE_SCHEMA_UNIT_SUMMARIES[unit]


def _has_unsafe_core_schema(
    schema: dict[str, Any], units: dict[_CoreSchemaUnit, None], *, is_unit_body: bool = False
) -> bool:
    """Whether the core schema itself makes its dumped values invalid input. Collect the units it refers to.

    That's the case for custom serializers, `Json`, `OnErrorOmit`, models that forbid extra keys but dump computed
    fields, models with a custom `__init__`, validators that get the raw input, and core schema types we don't know.
    """
    # References and models can carry serializers too, e.g. `Annotated[Model, PlainSerializer(...)]`,
    # so we check them before we summarize the schemas that they refer to
    if _is_custom_serialization(schema.get("serialization")):
        return True
    # `OnErrorOmit` and other defaults that swallow errors make invalid data pass, so we can't tell what the dumped
    # keys break
    if schema.get("on_error", "raise") != "raise":
        return True
    schema_type = schema["type"]
    if schema_type == "definition-ref":
        units["ref", schema["schema_ref"]] = None
        return False
    if _is_renamed_key_check(schema):
        return _has_unsafe_core_schema(schema["schema"], units, is_unit_body=is_unit_body)
    if schema_type == "model" and not is_unit_body:
        _CORE_SCHEMA_MODELS.setdefault(schema["cls"], schema)
        units["model", schema["cls"]] = None
        return False
    if (
        schema_type not in _CHILD_SCHEMA_KEYS
        or schema.get("custom_init")
        or (schema_type == "model" and _overrides_dumping(schema["cls"]))
        or (
            schema_type in ("model", "dataclass")
            and schema.get("config", {}).get("extra_fields_behavior") == "forbid"
            and _get_fields_core_schema(schema).get("computed_fields")
        )
    ):
        return True
    return any(_has_unsafe_core_schema(child, units) for child in _get_child_core_schemas(schema))


def _get_child_core_schemas(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for key, holds_several, is_optional in _CHILD_SCHEMA_KEYS[schema["type"]]:
        if is_optional and key not in schema:
            continue
        child = schema[key]
        if holds_several:
            for item in child.values() if isinstance(child, dict) else child:
                # Union choices can be `(schema, label)` tuples
                yield item[0] if isinstance(item, tuple) else item
        else:
            yield child


def _is_custom_serialization(serialization: object) -> bool:
    # Serializers that only run in JSON mode don't affect `model_dump()`
    return isinstance(serialization, dict) and serialization.get("when_used") not in ("json", "json-unless-none")


def _get_validation_paths(model: type[BaseModel], name: str, field: FieldInfo) -> list[list[Union[str, int]]]:
    """Return the paths in the input data that the field validates, in the order Pydantic tries them"""
    alias = field.validation_alias
    validate_by_alias, validate_by_name, _ = _get_validation_settings(model)
    paths: list[list[Union[str, int]]] = []
    if not validate_by_alias or alias is None:
        paths.append([name])
    else:
        # Pydantic only accepts strings, `AliasPath`, and `AliasChoices`, which this turns into lists of paths
        paths.extend(cast("list[list[Union[str, int]]]", _normalize_validation_alias(alias)))
    if validate_by_name:
        paths.append([name])
    return paths


def _get_validation_settings(model: type[BaseModel]) -> tuple[bool, bool, str]:
    """Return `(validate_by_alias, validate_by_name, extra)` of the model's config, resolved like pydantic does"""
    config = model.model_config
    validate_by_alias = config.get("validate_by_alias", True)
    validate_by_name = config.get("validate_by_name")
    if validate_by_name is None:
        populate_by_name = config.get("populate_by_name")
        validate_by_name = not validate_by_alias if populate_by_name is None else populate_by_name
    return validate_by_alias, validate_by_name, config.get("extra") or "ignore"


def _do_compiled_settings_match(model: type[BaseModel], compiled_config: object) -> bool:
    """Whether the compiled schema validates the model's keys with the settings of its config, which we plan with.

    A custom `__get_pydantic_core_schema__` can change them.
    """
    return isinstance(compiled_config, dict) and _get_validation_settings(model) == (
        compiled_config.get("validate_by_alias", True),
        compiled_config.get("validate_by_name", False),
        compiled_config.get("extra_fields_behavior", "ignore"),
    )
