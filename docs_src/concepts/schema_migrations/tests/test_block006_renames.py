"""Test what the page says about renaming a field that a schema inherits."""

from cadwyn import Version, VersionBundle, VersionChange, schema
from cadwyn.schema_generation import generate_versioned_models

from docs_src.concepts.schema_migrations.block006 import (
    BusinessInvoice,
    Invoice,
)


class RenameCodeInInvoice(VersionChange):
    description = "Renamed 'Invoice.code' to 'Invoice.number'"
    instructions_to_migrate_to_previous_version = (
        schema(Invoice).field("code").had(name="number"),
    )


class RenameCodeInBusinessInvoice(VersionChange):
    description = "Renamed 'BusinessInvoice.code' to 'BusinessInvoice.number'"
    instructions_to_migrate_to_previous_version = (
        schema(BusinessInvoice).field("code").had(name="number"),
    )


def _get_old_models(version_change: type[VersionChange]):
    versions = VersionBundle(
        Version("2001-01-01", version_change),
        Version("2000-01-01"),
    )
    return generate_versioned_models(versions)["2000-01-01"]


def test__inherited_field_had_name__in_declaring_schema__should_rename_field_in_subclasses():
    old_models = _get_old_models(RenameCodeInInvoice)
    assert list(old_models[Invoice].model_fields) == ["number"]
    assert list(old_models[BusinessInvoice].model_fields) == ["number"]


def test__inherited_field_had_name__in_subclass__should_keep_old_field_next_to_renamed_one():
    # The page tells users to rename the field in the schema that declares it
    # because of this. If this test fails, update "Fields that a schema inherits"
    old_models = _get_old_models(RenameCodeInBusinessInvoice)
    assert list(old_models[BusinessInvoice].model_fields) == ["code", "number"]
    assert list(old_models[Invoice].model_fields) == ["code"]
