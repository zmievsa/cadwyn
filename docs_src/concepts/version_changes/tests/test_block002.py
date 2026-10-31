from docs_src.concepts.version_changes.block001 import RemoveTaxIdEndpoints
from docs_src.concepts.version_changes.block002 import versions


def test__version_bundle__with_remove_tax_id_endpoints__should_add_the_change_to_2023_02_10():
    changes_by_version = {
        version.value: version.changes for version in versions
    }

    assert changes_by_version == {
        "2023-02-10": (RemoveTaxIdEndpoints,),
        "2022-11-16": (),
    }
