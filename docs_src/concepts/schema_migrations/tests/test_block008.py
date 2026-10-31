import pytest
from cadwyn import Version, VersionBundle
from cadwyn.schema_generation import generate_versioned_models
from pydantic import ValidationError

from docs_src.concepts.schema_migrations.block008 import (
    AddFooValidatorToMySchema,
)
from docs_src.concepts.schema_migrations.head.with_foo_validator import MySchema

# Cadwyn validates every request with HEAD schemas after it migrates the
# request, so the older version only changes the schemas themselves
models = generate_versioned_models(
    VersionBundle(
        Version("2001-01-01", AddFooValidatorToMySchema),
        Version("2000-01-01"),
    )
)


def test__validator_didnt_exist__older_schema__should_accept_value_without_colon():
    assert models["2000-01-01"][MySchema](foo="foo").foo == "foo"


def test__validator_didnt_exist__newer_schema__should_reject_value_without_colon():
    with pytest.raises(ValidationError, match="foo must contain a colon"):
        models["2001-01-01"][MySchema](foo="foo")


def test__validator_didnt_exist__newer_schema__should_accept_value_with_colon():
    assert models["2001-01-01"][MySchema](foo="a:b").foo == "a:b"
