import pytest
from cadwyn import Version, VersionBundle
from cadwyn.schema_generation import generate_versioned_models
from pydantic import ValidationError

from docs_src.concepts.schema_migrations.block012 import (
    Dimensions,
    RequirePositiveWidth,
)

models = generate_versioned_models(
    VersionBundle(
        Version("2001-01-01", RequirePositiveWidth),
        Version("2000-01-01"),
    )
)


def test__assigned_validator_didnt_exist__older_schema__should_accept_negative_width():
    assert models["2000-01-01"][Dimensions](
        width=-1, height=2
    ).model_dump() == {
        "width": -1,
        "height": 2,
    }


def test__assigned_validator_didnt_exist__older_schema__should_still_reject_negative_height():
    with pytest.raises(ValidationError, match=r"height.*\n.*must be positive"):
        models["2000-01-01"][Dimensions](width=-1, height=-2)


def test__assigned_validator_didnt_exist__newer_schema__should_reject_negative_width():
    with pytest.raises(ValidationError, match=r"width.*\n.*must be positive"):
        models["2001-01-01"][Dimensions](width=-1, height=2)


def test__assigned_validator_didnt_exist__newer_schema__should_accept_positive_dimensions():
    assert models["2001-01-01"][Dimensions](width=1, height=2).model_dump() == {
        "width": 1,
        "height": 2,
    }
