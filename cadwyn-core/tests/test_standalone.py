"""cadwyn-core works when it's installed on its own, without cadwyn, FastAPI, or Starlette.

The `standalone` tox environment runs these tests with nothing installed but the cadwyn-core wheel, its requirements,
and pytest.
"""

import importlib.util
from pathlib import Path

import pytest
from pydantic import BaseModel

import cadwyn_core
from cadwyn_core import (
    MigratedRequest,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    generate_versioned_models,
    migrate_request,
    migrate_response,
    schema,
)
from cadwyn_core.exceptions import CadwynError


@pytest.mark.parametrize("package", ["cadwyn", "fastapi", "starlette"])
def test__environment__should_not_have_the_packages_that_cadwyn_core_does_without(package: str):
    assert importlib.util.find_spec(package) is None


def test__environment__should_have_cadwyn_core_installed_rather_than_its_source():
    assert "site-packages" in Path(cadwyn_core.__file__).parts


@pytest.mark.parametrize("name", cadwyn_core.__all__)
def test__public_names__should_be_importable(name: str):
    assert getattr(cadwyn_core, name) is not None


class User(BaseModel):
    full_name: str


class RenameNameToFullName(VersionChange):
    description = "'name' is now called 'full_name'"
    instructions_to_migrate_to_previous_version = (schema(User).field("full_name").had(name="name"),)

    @convert_request_to_next_version_for(User)
    def rename_name_to_full_name(request: RequestInfo):
        request.body["full_name"] = request.body.pop("name")

    @convert_response_to_previous_version_for(User)
    def rename_full_name_to_name(response: ResponseInfo):
        response.body["name"] = response.body.pop("full_name")


versions = VersionBundle(Version("2001-01-01", RenameNameToFullName), Version("2000-01-01"))


def test__generate_versioned_models__should_generate_the_models_of_older_versions():
    assert list(generate_versioned_models(versions)["2000-01-01"][User].model_fields) == ["name"]


def test__migrate_request__should_migrate_the_request_to_head():
    migrated = migrate_request(versions, User, body={"name": "John"}, version="2000-01-01", cookies={"a": "b"})

    assert migrated == MigratedRequest(body=User(full_name="John"), headers=None, cookies={"a": "b"}, query_params={})


def test__migrate_response__should_migrate_the_response_to_the_older_version():
    migrated = migrate_response(versions, User, body=User(full_name="John"), version="2000-01-01", status_code=201)

    assert migrated.body.model_dump() == {"name": "John"}
    assert (migrated.status_code, migrated.headers) == (201, None)


def test__migrate_request__with_headers__should_name_starlette_that_headers_need():
    with pytest.raises(CadwynError, match="Migrating headers needs Starlette"):
        migrate_request(versions, User, body={"name": "John"}, version="2000-01-01", headers={"a": "b"})
