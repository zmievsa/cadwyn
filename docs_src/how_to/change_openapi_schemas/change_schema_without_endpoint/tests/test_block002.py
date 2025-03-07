from fastapi.testclient import TestClient

from cadwyn import Cadwyn, Version, VersionBundle
from docs_src.how_to.change_openapi_schemas.change_schema_without_endpoint.block002 import (
    ChangeUserIDToString,
)

versions = VersionBundle(
    Version("2023-04-12", ChangeUserIDToString), Version("2022-11-16")
)
app = Cadwyn(versions=versions)


def test__migrate_to_previous_version__with_check_usage_set_to_false__should_not_raise_error():
    with TestClient(app):
        ...
