import pytest

from cadwyn import Cadwyn, Version, VersionBundle
from cadwyn.exceptions import (
    RouteResponseBySchemaConverterDoesNotApplyToAnythingError,
)
from docs_src.how_to.change_openapi_schemas.change_schema_without_endpoint.block001 import (
    ChangeUserIDToString,
)

versions = VersionBundle(
    Version("2023-04-12", ChangeUserIDToString), Version("2022-11-16")
)
app = Cadwyn(versions=versions)


def test__migrate_to_previous_version__without_check_usage_argument__should_raise_error():
    with pytest.raises(
        RouteResponseBySchemaConverterDoesNotApplyToAnythingError
    ):
        app._cadwyn_initialize()
