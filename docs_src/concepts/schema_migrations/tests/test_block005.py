import runpy
import warnings

import pytest
from cadwyn import Version, VersionBundle, VersionChange, schema
from cadwyn.schema_generation import generate_versioned_models
from pydantic import BaseModel, Field


class MySchema(BaseModel):
    foo: str = Field(alias="foo_key")


class RenameFooKey(VersionChange):
    description = "Renamed the key of 'MySchema.foo' to 'foo_key'"
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(alias="old_foo_key"),
    )


versions = VersionBundle(
    Version("2001-01-01", RenameFooKey),
    Version("2000-01-01"),
)


def test__alias_change__without_warning_filter__should_emit_future_warning():
    with pytest.warns(FutureWarning, match="changes the alias of"):
        generate_versioned_models(versions)


def test__alias_change__with_warning_filter__should_not_emit_future_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        runpy.run_module("docs_src.concepts.schema_migrations.block005")

        models = generate_versioned_models(versions)

    # The filter only silences the warning: the older version keeps the old key
    old_schema = models["2000-01-01"][MySchema]
    assert old_schema.model_validate({"foo_key": "foo"}).foo == "foo"
