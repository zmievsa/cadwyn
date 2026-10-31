from cadwyn import VersionChangeWithSideEffects


class RejectUserAddressesMissingFromExternalService(
    VersionChangeWithSideEffects
):
    """User addresses are now verified by an external service during creation;
    invalid addresses return 400 to prevent storing undeliverable locations.
    """

    instructions_to_migrate_to_previous_version = ()
