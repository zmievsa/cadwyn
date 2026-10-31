from cadwyn import Version, VersionBundle, VersionChange


class ChangeCreateLogic(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


class AddRequiredFields(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


class DeleteEndpoint(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


class ChangeFields(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


class RenameFields(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


class RenameEndpoints(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


class RefactorFields(VersionChange):
    """A poorly named version change."""

    instructions_to_migrate_to_previous_version = ()


versions = VersionBundle(
    Version("2023-05-09", ChangeCreateLogic, AddRequiredFields),
    Version("2023-04-02", DeleteEndpoint, ChangeFields, RenameFields),
    Version("2023-02-10", RenameEndpoints, RefactorFields),
    Version("2022-11-16"),
)
