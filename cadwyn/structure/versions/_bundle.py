import functools
from collections.abc import (
    Callable,  # Needed by runtime annotation introspection.
    Iterator,
)
from contextvars import ContextVar
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Union,
)

from pydantic import BaseModel  # noqa: TC002 # Needed by runtime annotation introspection.

from cadwyn._utils import is_enum_looked_up_by_value
from cadwyn.exceptions import (
    CadwynError,
    CadwynStructureError,
)
from cadwyn.structure.common import VersionedModel, VersionType
from cadwyn.structure.data import ResponseInfo

from ._changes import HeadVersion, IdentifierPythonPath, Version, VersionChange, get_cls_pythonpath
from ._migration_targets import (
    _get_models_of_schema,
    _SchemaMigrationTarget,  # Needed by runtime annotation introspection.
)
from ._migrations import _migrate_response

if TYPE_CHECKING:
    from fastapi.routing import APIRoute


class VersionBundle:
    def __init__(
        self,
        latest_version_or_head_version: Union[Version, HeadVersion],
        /,
        *other_versions: Version,
        api_version_var: Union[ContextVar[Union[VersionType, None]], None] = None,
    ) -> None:
        super().__init__()

        if isinstance(latest_version_or_head_version, HeadVersion):
            self.head_version = latest_version_or_head_version
            self.versions = other_versions
        else:
            self.head_version = HeadVersion()
            self.versions = (latest_version_or_head_version, *other_versions)
        self.reversed_versions = tuple(reversed(self.versions))

        if api_version_var is None:
            api_version_var = ContextVar("cadwyn_api_version")

        self.version_values = tuple(version.value for version in self.versions)
        self.reversed_version_values = tuple(reversed(self.version_values))
        self.api_version_var = api_version_var
        self._all_versions = (self.head_version, *self.versions)
        self._version_changes_to_version_mapping = {
            version_change: version.value for version in self.versions for version_change in version.changes
        }
        self._version_values_set: set[str] = set()
        for version in self.versions:
            if version.value not in self._version_values_set:
                self._version_values_set.add(version.value)
            else:
                raise CadwynStructureError(
                    f"You tried to define two versions with the same value in the same "
                    f"{VersionBundle.__name__}: '{version.value}'.",
                )
            for version_change in version.changes:
                if version_change._bound_version_bundle is not None:
                    raise CadwynStructureError(
                        f"You tried to bind version change '{version_change.__name__}' to two different versions. "
                        "It is prohibited.",
                    )
                version_change._bound_version_bundle = self
        if not self.versions:
            raise CadwynStructureError("You must define at least one non-head version in a VersionBundle.")

        if self.versions[-1].changes:
            raise CadwynStructureError(
                f'The first version "{self.versions[-1].value}" cannot have any version changes. '
                "Version changes are defined to migrate to/from a previous version so you "
                "cannot define one for the very first version.",
            )

    def __iter__(self) -> Iterator[Version]:
        yield from self.versions

    @functools.cached_property
    def versioned_schemas(self) -> dict[IdentifierPythonPath, type[VersionedModel]]:
        altered_schemas = {
            get_cls_pythonpath(instruction.schema): instruction.schema
            for version in self._all_versions
            for version_change in version.changes
            for instruction in list(version_change.alter_schema_instructions)
        }

        # Migrations for a union (e.g. `Union[A, B]`) are migrations for its members
        migrated_schemas = {
            get_cls_pythonpath(model): model
            for version in self._all_versions
            for version_change in version.changes
            for schema in list(version_change.alter_request_by_schema_instructions.keys())
            for model in _get_models_of_schema(schema)
        }

        return altered_schemas | migrated_schemas

    @functools.cached_property
    def _all_changes(self) -> tuple[type["VersionChange"], ...]:
        return tuple(version_change for version in self.versions for version_change in version.changes)

    @functools.cached_property
    def versioned_enums(self) -> dict[IdentifierPythonPath, type[Enum]]:
        return {
            get_cls_pythonpath(instruction.enum): instruction.enum
            for version in self._all_versions
            for version_change in version.changes
            for instruction in version_change.alter_enum_instructions
        }

    @functools.cached_property
    def _has_versioned_plain_enums(self) -> bool:
        """Whether a plain enum or flag has enum instructions, so that the head rejects the members of its copies.

        Pydantic looks up the members of `str`, `int`, and `float` enums by their values, so the head accepts
        the members of their copies.
        """
        return any(not is_enum_looked_up_by_value(enum) for enum in self.versioned_enums.values())

    def _get_closest_lesser_version(self, version: VersionType):
        for defined_version in self.version_values:
            if defined_version <= version:
                return defined_version
        raise CadwynError("You tried to migrate to version that is earlier than the first version which is prohibited.")

    def _migrate_response(
        self,
        response_info: ResponseInfo,
        current_version: VersionType,
        head_route: "Union[APIRoute, None]",
        response_target: "_SchemaMigrationTarget",
        get_response_union_member: "Union[Callable[[], Union[type[BaseModel], None]], None]" = None,
    ) -> ResponseInfo:
        return _migrate_response(
            self,
            response_info,
            current_version,
            head_route,
            response_target,
            get_response_union_member,
        )
