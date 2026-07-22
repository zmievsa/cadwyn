from fastapi.testclient import TestClient

import tests._resources.namespace_package as namespace_package
from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    generate_versioned_models,
    schema,
)
from tests._resources.namespace_package.schemas import NamespacePackageSchema


def test__namespace_package_models_work_in_schema_and_router_generation():
    """Regression test for https://github.com/zmievsa/cadwyn/issues/160.

    The issue predates runtime schema generation, when Cadwyn accepted a schemas package directly.
    Models now reach VersionBundle through version change instructions.
    """
    assert namespace_package.__file__ is None

    class MyVersionChange(VersionChange):
        description = "Change value from an integer to a string"
        instructions_to_migrate_to_previous_version = (
            schema(NamespacePackageSchema).field("value").had(type=int),
            schema(NamespacePackageSchema).field("value").didnt_have("coerce_numbers_to_str"),
        )

    versions = VersionBundle(Version("2001-01-01", MyVersionChange), Version("2000-01-01"))

    versioned_models = generate_versioned_models(versions)
    assert versioned_models["2000-01-01"][NamespacePackageSchema].model_validate({"value": 1}).model_dump() == {
        "value": 1
    }
    assert versioned_models["2001-01-01"][NamespacePackageSchema].model_validate({"value": 1}).model_dump() == {
        "value": "1"
    }

    router = VersionedAPIRouter()

    @router.post("/test")
    async def test_route(payload: NamespacePackageSchema) -> NamespacePackageSchema:
        return payload

    app = Cadwyn(versions=versions)
    app.generate_and_include_versioned_routers(router)

    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.post("/test", json={"value": 1}).json() == {"value": 1}
    assert client_2001.post("/test", json={"value": 1}).json() == {"value": "1"}
