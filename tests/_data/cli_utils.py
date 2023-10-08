from contextvars import ContextVar
from datetime import date

from cadwyn.structure import Version, VersionBundle, VersionChange, schema

from . import latest

api_version_var = ContextVar("api_version")


class VersionChange1(VersionChange):
    description = "..."
    instructions_to_migrate_to_previous_version = [
        schema(latest.SchemaWithOneStrField).field("foo").didnt_exist,
    ]


version_bundle = VersionBundle(
    Version(date(2001, 1, 1), VersionChange1),
    Version(date(2000, 1, 1)),
    api_version_var=api_version_var,
)


def callable_that_returns_version_bundle():
    return version_bundle


def invalid_callable_that_has_arguments(_arg):
    raise NotImplementedError


def invalid_callable_that_returns_non_version_bundle():
    return 83
