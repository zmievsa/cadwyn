from cadwyn import Version, VersionBundle, generate_versioned_models

from docs_src.concepts.enum_migrations.block003 import (
    Color,
    RemoveBlueFromColor,
)


def test__remove_blue_from_color__in_old_version__should_keep_the_labels_of_the_other_members():
    versions = VersionBundle(
        Version("2023-02-10", RemoveBlueFromColor),
        Version("2022-11-16"),
    )

    old_color = generate_versioned_models(versions)["2022-11-16"][Color]

    assert [(member.value, member.label) for member in old_color] == [
        ("red", "Red"),
        ("green", "Green"),
    ]
