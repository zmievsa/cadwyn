from cadwyn import VersionChangeWithSideEffects

from docs_src.concepts.version_changes.block015 import (
    RejectUserAddressesMissingFromExternalService,
)


def test__reject_user_addresses_missing_from_external_service__should_be_a_version_change_with_side_effects():
    assert issubclass(
        RejectUserAddressesMissingFromExternalService,
        VersionChangeWithSideEffects,
    )
