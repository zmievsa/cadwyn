from datetime import date

import pytest
from pydantic import Field

from cadwyn.exceptions import CadwynError
from cadwyn.schema_generation import migrate_response_body
from cadwyn.structure.data import ResponseInfo, convert_response_to_previous_version_for
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle
from tests.conftest import version_change
from tests.test_data_migrations import EmptySchema


def test__version_with_date__should_be_converted_to_string():
    assert Version(date(2022, 11, 16)).value == "2022-11-16"


def test__manual_response_migrations__with_version_as_date():
    @convert_response_to_previous_version_for(EmptySchema)
    def response_converter(response: ResponseInfo):
        response.body["amount"] = 83

    version_bundle = VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                schema(EmptySchema).field("name").existed_as(type=str, info=Field(default="Apples")),
                schema(EmptySchema).field("amount").existed_as(type=int),
                convert=response_converter,
            ),
        ),
        Version("2000-01-01"),
    )

    new_response = migrate_response_body(
        version_bundle, EmptySchema, latest_body={"id": "hewwo"}, version=date(2000, 1, 1)
    )
    assert new_response.model_dump() == {
        "name": "Apples",
        "amount": 83,
    }
    assert new_response.model_dump(exclude_unset=True) == {"amount": 83}

    with pytest.raises(CadwynError):
        new_response = migrate_response_body(
            version_bundle, EmptySchema, latest_body={"id": "hewwo"}, version=date(1999, 1, 1)
        )
