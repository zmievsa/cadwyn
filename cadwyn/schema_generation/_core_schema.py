from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
)

from pydantic import BaseModel

if TYPE_CHECKING:
    import collections.abc

# The keys of the core schemas whose values are not schemas, such as default values, which can be anything
_CORE_SCHEMA_KEYS_WITHOUT_SCHEMAS = frozenset({"default", "default_factory", "expected", "metadata"})


def _iter_core_schema_nodes(
    core_schema: Any,
    *,
    definitions: Union[dict[str, Any], None] = None,
    should_skip_children: Union[Callable[[dict[str, Any]], bool], None] = None,
) -> "collections.abc.Iterator[dict[str, Any]]":
    """Iterate over the schemas within the core schema, following its definition refs if definitions are given"""
    nodes_to_visit = [core_schema]
    visited_ids: set[int] = set()
    while nodes_to_visit:
        node = nodes_to_visit.pop()
        if id(node) in visited_ids:
            continue
        visited_ids.add(id(node))
        if isinstance(node, dict):
            yield node
            if should_skip_children is not None and should_skip_children(node):
                continue
            if definitions is not None and node.get("type") == "definition-ref":
                nodes_to_visit.append(definitions.get(node.get("schema_ref")))
            nodes_to_visit.extend(value for key, value in node.items() if key not in _CORE_SCHEMA_KEYS_WITHOUT_SCHEMAS)
        elif isinstance(node, list | tuple):
            nodes_to_visit.extend(node)


def _get_classes_in_core_schema(
    core_schema: Any,
    *,
    definitions: Union[dict[str, Any], None] = None,
    skipped_classes: "collections.abc.Set[type]" = frozenset(),
) -> set[type]:
    """Return the classes that the core schema validates or serializes, e.g. the models in its fields.

    We do not look into the schemas of skipped_classes.
    """

    def should_skip_children(node: dict[str, Any]) -> bool:
        cls = node.get("cls")
        return isinstance(cls, type) and cls in skipped_classes

    return {
        node["cls"]
        for node in _iter_core_schema_nodes(
            core_schema, definitions=definitions, should_skip_children=should_skip_children
        )
        if isinstance(node.get("cls"), type)
    }


def _get_core_schema_definitions(core_schema: Any) -> dict[str, Any]:
    return {node["ref"]: node for node in _iter_core_schema_nodes(core_schema) if isinstance(node.get("ref"), str)}


def _get_field_core_schemas(
    model: type[BaseModel], core_schema: Any, definitions: dict[str, Any]
) -> Union[dict[str, Any], None]:
    """Return the core schemas of the fields of the model, or None if we cannot find them (e.g. for RootModel)"""
    for node in _iter_core_schema_nodes(core_schema, definitions=definitions):
        if node.get("type") == "model" and node.get("cls") is model:
            for inner_node in _iter_core_schema_nodes(
                node.get("schema"), definitions=definitions, should_skip_children=lambda n: n.get("type") == "model"
            ):
                if inner_node.get("type") == "model-fields":
                    return {name: field.get("schema") for name, field in inner_node["fields"].items()}
            return None
    return None  # pragma: no cover # The schema of a model always describes the model itself
