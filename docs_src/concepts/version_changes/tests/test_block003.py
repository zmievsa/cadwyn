from docs_src.concepts.version_changes.block003 import versions


def test__version_bundle__with_poorly_named_version_changes__should_list_their_names_by_version():
    names_by_version = {
        version.value: [change.__name__ for change in version.changes]
        for version in versions
    }

    assert names_by_version == {
        "2023-05-09": ["ChangeCreateLogic", "AddRequiredFields"],
        "2023-04-02": ["DeleteEndpoint", "ChangeFields", "RenameFields"],
        "2023-02-10": ["RenameEndpoints", "RefactorFields"],
        "2022-11-16": [],
    }
