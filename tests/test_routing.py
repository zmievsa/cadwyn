import re
from collections.abc import Awaitable, Callable
from datetime import date
from types import ModuleType
from typing import Annotated, Any, NewType, TypeAlias, cast, get_args

import pytest
from fastapi import APIRouter, Body, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.responses import FileResponse

from tests._data import latest
from tests._data.latest import some_schema
from tests.test_codegen import (
    generate_test_version_packages,
)

# TODO: It's bad to import between tests like that
from universi import VersionBundle, VersionedAPIRouter
from universi.exceptions import RouterGenerationError
from universi.structure import Version, endpoint, schema
from universi.structure.endpoints import AlterEndpointSubInstruction
from universi.structure.enums import AlterEnumSubInstruction, enum
from universi.structure.schemas import AlterSchemaSubInstruction
from universi.structure.versions import VersionChange
from tests._data.unversioned_schema_dir.unversioned_schemas import UnversionedSchema1
from tests._data.unversioned_schema_dir import UnversionedSchema2
from tests._data.unversioned_schemas import UnversionedSchema3

Endpoint: TypeAlias = Callable[..., Awaitable[Any]]


@pytest.fixture()
def router() -> VersionedAPIRouter:
    return VersionedAPIRouter()


@pytest.fixture()
def test_path() -> str:
    return "/test/{hewoo}"


@pytest.fixture()
def test_endpoint(router: VersionedAPIRouter, test_path: str) -> Endpoint:
    @router.get(test_path)
    async def test(hewwo: int):
        raise NotImplementedError

    return test


def client(router: APIRouter) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def create_versioned_copies(
    router: VersionedAPIRouter,
    *instructions: AlterSchemaSubInstruction | AlterEndpointSubInstruction | AlterEnumSubInstruction,
    latest_schemas_module: ModuleType | None = None,
) -> dict[date, VersionedAPIRouter]:
    class MyVersionChange(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = instructions

    return router.create_versioned_copies(
        VersionBundle(
            Version(date(2001, 1, 1), MyVersionChange),
            Version(date(2000, 1, 1)),
        ),
        latest_schemas_module=latest_schemas_module,
    )


def create_versioned_api_routes(
    router: VersionedAPIRouter,
    *instructions: AlterSchemaSubInstruction | AlterEndpointSubInstruction | AlterEnumSubInstruction,
    latest_schemas_module: ModuleType | None = None,
) -> tuple[list[APIRoute], list[APIRoute]]:
    routers = create_versioned_copies(
        router,
        *instructions,
        latest_schemas_module=latest_schemas_module,
    )
    for router in routers.values():
        for route in router.routes:
            assert isinstance(route, APIRoute)
    return cast(
        tuple[list[APIRoute], list[APIRoute]],
        (routers[date(2000, 1, 1)].routes, routers[date(2001, 1, 1)].routes),
    )


def test__router_generation__forgot_to_generate_schemas__error(
    router: VersionedAPIRouter,
):
    with pytest.raises(
        RouterGenerationError,
        match="Versioned schema directory '.+' does not exist.",
    ):
        create_versioned_api_routes(router, latest_schemas_module=latest)


def test__endpoint_didnt_exist(router: VersionedAPIRouter, test_endpoint: Endpoint, test_path: str):
    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        endpoint(test_path, ["GET"]).didnt_exist,
    )

    assert routes_2000 == []
    assert len(routes_2001) == 1
    assert routes_2001[0].endpoint.func == test_endpoint


# TODO: Add a test for removing an endpoint and adding it back
def test__endpoint_existed(router: VersionedAPIRouter):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    @router.post("/test")
    async def test_endpoint_post():
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        endpoint("/test", ["GET"]).existed,
    )

    assert len(routes_2000) == 2
    assert routes_2000[0].endpoint.func == test_endpoint_post
    assert routes_2000[1].endpoint.func == test_endpoint

    assert len(routes_2001) == 1
    assert routes_2001[0].endpoint.func == test_endpoint_post


@pytest.mark.parametrize(
    ("attr", "attr_value"),
    [
        ("path", "/wow"),
        ("status_code", 204),
        ("tags", ["foo", "bar"]),
        ("summary", "my summary"),
        ("description", "my description"),
        ("response_description", "my response description"),
        ("deprecated", True),
        ("include_in_schema", False),
        ("name", "my name"),
        ("openapi_extra", {"my_openapi_extra": "openapi_extra"}),
        ("responses", {405: {"description": "hewwo"}, 500: {"description": "hewwo1"}}),
        ("methods", ["GET", "POST"]),
        ("operation_id", "my_operation_id"),
        ("response_class", FileResponse),
        ("dependencies", [Depends(lambda: "hewwo")]),  # pragma: no cover
        (
            "generate_unique_id_function",
            lambda api_route: api_route.endpoint.__name__,
        ),  # pragma: no cover
    ],
)
def test__endpoint_had(
    router: VersionedAPIRouter,
    attr: str,
    attr_value: Any,
    test_endpoint: Endpoint,
    test_path: str,
):
    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        endpoint(test_path, ["GET"]).had(**{attr: attr_value}),
    )

    assert len(routes_2000) == len(routes_2001) == 1
    assert getattr(routes_2000[0], attr) == attr_value
    assert getattr(routes_2001[0], attr) != attr_value


def test__endpoint_only_exists_in_older_versions__endpoint_is_not_a_route__error(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
):
    with pytest.raises(
        LookupError,
        match=re.escape("Route not found on endpoint: 'test2'"),
    ):

        @router.only_exists_in_older_versions
        async def test2():
            raise NotImplementedError


def test__router_generation__non_api_route_added(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
):
    @router.websocket("/test2")
    async def test_websocket():
        raise NotImplementedError

    routers = create_versioned_copies(router, endpoint(test_path, ["GET"]).didnt_exist)
    assert len(routers[date(2000, 1, 1)].routes) == 1
    assert len(routers[date(2001, 1, 1)].routes) == 2
    route = routers[date(2001, 1, 1)].routes[0]
    assert isinstance(route, APIRoute)
    assert route.endpoint.func == test_endpoint


def test__router_generation__creating_a_synchronous_endpoint__error(
    router: VersionedAPIRouter,
):
    @router.get("/test")
    def test():
        raise NotImplementedError

    with pytest.raises(
        TypeError,
        match=re.escape("All versioned endpoints must be asynchronous."),
    ):
        create_versioned_copies(router, endpoint("/test", ["GET"]).didnt_exist)


def test__router_generation__changing_a_deleted_endpoint__error(
    router: VersionedAPIRouter,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test():
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Endpoint '/test' you tried to delete in 'MyVersionChange' doesn't exist in new version",
        ),
    ):
        create_versioned_copies(router, endpoint("/test", ["GET"]).had(description="Hewwo"))


def test__router_generation__deleting_a_deleted_endpoint__error(
    router: VersionedAPIRouter,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test():
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Endpoint '/test' you tried to delete in 'MyVersionChange' doesn't exist in new version",
        ),
    ):
        create_versioned_copies(router, endpoint("/test", ["GET"]).didnt_exist)


def test__router_generation__re_creating_an_existing_endpoint__error(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Endpoint '/test/{hewoo}' you tried to re-create in 'MyVersionChange' already existed in newer versions",
        ),
    ):
        create_versioned_copies(router, endpoint(test_path, ["GET"]).existed)


def get_nested_field_type(annotation: Any) -> type[BaseModel]:
    return get_args(get_args(annotation)[1])[0].__fields__["foo"].type_.__fields__["foo"].annotation


def test__router_generation__re_creating_a_non_endpoint__error(
    router: VersionedAPIRouter,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Endpoint '/test' you tried to re-create in 'MyVersionChange' wasn't among the deleted routes",
        ),
    ):
        create_versioned_copies(router, endpoint("/test", ["GET"]).existed)


def test__router_generation__changing_attribute_to_the_same_value__error(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Expected attribute 'path' of endpoint 'test' to be different in 'MyVersionChange', but it was the same."
            " It means that your version change has no effect on the attribute and can be removed.",
        ),
    ):
        create_versioned_copies(router, endpoint(test_path, ["GET"]).had(path=test_path))


def test__router_generation__non_api_route_added_with_schemas(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
):
    @router.websocket("/test2")
    async def test_websocket():
        raise NotImplementedError

    generate_test_version_packages()
    routers = create_versioned_copies(
        router,
        endpoint(test_path, ["GET"]).didnt_exist,
        latest_schemas_module=latest,
    )
    assert len(routers[date(2000, 1, 1)].routes) == 1
    assert len(routers[date(2001, 1, 1)].routes) == 2
    route = routers[date(2001, 1, 1)].routes[0]
    assert isinstance(route, APIRoute)
    assert route.endpoint.func == test_endpoint


def test__router_generation__updating_response_model_when_schema_is_defined_in_a_non_init_file(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    @router.get("/test", response_model=some_schema.MySchema)
    async def test():
        raise NotImplementedError

    instruction = schema(some_schema.MySchema).field("foo").had(type=str)
    generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        instruction,
        latest_schemas_module=latest,
    )
    assert routes_2000[0].response_model.__fields__["foo"].annotation == str
    assert routes_2001[0].response_model.__fields__["foo"].annotation == int


def test__router_generation__updating_response_model(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    @router.get(
        "/test",
        response_model=dict[str, list[latest.SchemaWithOnePydanticField]],
    )
    async def test():
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    schemas_2000, schemas_2001 = generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        instruction,
        latest_schemas_module=latest,
    )
    assert len(routes_2000) == len(routes_2001) == 1
    assert routes_2000[0].response_model == dict[str, list[schemas_2000.SchemaWithOnePydanticField]]
    assert routes_2001[0].response_model == dict[str, list[schemas_2001.SchemaWithOnePydanticField]]

    assert get_nested_field_type(routes_2000[0].response_model) == list[str]
    assert get_nested_field_type(routes_2001[0].response_model) == int


def test__router_generation__updating_request_models(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    @router.get("/test")
    async def test(body: dict[str, list[latest.SchemaWithOnePydanticField]]):
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    schemas_2000, schemas_2001 = generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        instruction,
        latest_schemas_module=latest,
    )
    assert len(routes_2000) == len(routes_2001) == 1
    assert (
        routes_2000[0].dependant.body_params[0].annotation == dict[str, list[schemas_2000.SchemaWithOnePydanticField]]
    )
    assert (
        routes_2001[0].dependant.body_params[0].annotation == dict[str, list[schemas_2001.SchemaWithOnePydanticField]]
    )

    assert get_nested_field_type(routes_2000[0].dependant.body_params[0].annotation) == list[str]
    assert get_nested_field_type(routes_2001[0].dependant.body_params[0].annotation) == int


def test__router_generation__using_unversioned_models(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    @router.get("/test")
    async def test1(body: UnversionedSchema1):
        raise NotImplementedError

    @router.get("/test2")
    async def test2(body: UnversionedSchema2):
        raise NotImplementedError

    @router.get("/test3")
    async def test3(body: UnversionedSchema3):
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        instruction,
        latest_schemas_module=latest,
    )

    assert len(routes_2000) == len(routes_2001) == 3
    assert routes_2000[0].dependant.body_params[0].type_ is UnversionedSchema1
    assert routes_2001[0].dependant.body_params[0].type_ is UnversionedSchema1

    assert routes_2000[1].dependant.body_params[0].type_ is UnversionedSchema2
    assert routes_2001[1].dependant.body_params[0].type_ is UnversionedSchema2

    assert routes_2000[2].dependant.body_params[0].type_ is UnversionedSchema3
    assert routes_2001[2].dependant.body_params[0].type_ is UnversionedSchema3


def test__router_generation__using_weird_typehints(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    newtype = NewType("newtype", str)

    @router.get("/test")
    async def test(param1: newtype = Body(), param2: str | int = Body()):
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(
        router,
        instruction,
        latest_schemas_module=latest,
    )
    assert len(routes_2000) == len(routes_2001) == 1
    assert routes_2000[0].dependant.body_params[0].annotation is newtype
    assert routes_2001[0].dependant.body_params[0].annotation is newtype

    assert routes_2000[0].dependant.body_params[1].annotation == str | int
    assert routes_2001[0].dependant.body_params[1].annotation == str | int


# TODO: This test should become multiple tests
def test__router_generation__updating_request_depends(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    def sub_dependency1(my_enum: latest.StrEnum) -> latest.StrEnum:
        return my_enum

    def dependency1(dep: latest.StrEnum = Depends(sub_dependency1)):  # noqa: B008
        return dep

    def sub_dependency2(my_enum: latest.StrEnum) -> latest.StrEnum:
        return my_enum

    # TODO: What if "a" gets deleted?
    def dependency2(
        dep: Annotated[latest.StrEnum, Depends(sub_dependency2)] = latest.StrEnum.a,
    ):
        return dep

    @router.get("/test1")
    async def test_with_dep1(dep: latest.StrEnum = Depends(dependency1)):  # noqa: B008
        return dep

    @router.get("/test2")
    async def test_with_dep2(dep: latest.StrEnum = Depends(dependency2)):  # noqa: B008
        return dep

    instruction = enum(latest.StrEnum).had(foo="bar")
    generate_test_version_packages(instruction)

    routers = create_versioned_copies(router, instruction, latest_schemas_module=latest)
    app_2000 = FastAPI()
    app_2001 = FastAPI()
    app_2000.include_router(routers[date(2000, 1, 1)])
    app_2001.include_router(routers[date(2001, 1, 1)])
    client_2000 = TestClient(app_2000)
    client_2001 = TestClient(app_2001)
    assert client_2000.get("/test1", params={"my_enum": "bar"}).json() == "bar"
    assert client_2000.get("/test2", params={"my_enum": "bar"}).json() == "bar"

    assert client_2001.get("/test1", params={"my_enum": "bar"}).json() == {
        "detail": [
            {
                "loc": ["query", "my_enum"],
                "msg": "value is not a valid enumeration member; permitted: '1'",
                "type": "type_error.enum",
                "ctx": {"enum_values": ["1"]},
            },
        ],
    }

    assert client_2001.get("/test2", params={"my_enum": "bar"}).json() == {
        "detail": [
            {
                "loc": ["query", "my_enum"],
                "msg": "value is not a valid enumeration member; permitted: '1'",
                "type": "type_error.enum",
                "ctx": {"enum_values": ["1"]},
            },
        ],
    }


def test__router_generation__updating_unused_dependencies(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
):
    def dependency(my_enum: latest.StrEnum):
        return my_enum

    @router.get("/test", dependencies=[Depends(dependency)])
    async def test_with_dep():
        pass

    instruction = enum(latest.StrEnum).had(foo="bar")
    generate_test_version_packages(instruction)

    routers = create_versioned_copies(router, instruction, latest_schemas_module=latest)
    client_2000 = client(routers[date(2000, 1, 1)])
    client_2001 = client(routers[date(2001, 1, 1)])
    assert client_2000.get("/test", params={"my_enum": "bar"}).json() is None

    assert client_2001.get("/test", params={"my_enum": "bar"}).json() == {
        "detail": [
            {
                "loc": ["query", "my_enum"],
                "msg": "value is not a valid enumeration member; permitted: '1'",
                "type": "type_error.enum",
                "ctx": {"enum_values": ["1"]},
            },
        ],
    }


def test__cascading_router_exists(router: VersionedAPIRouter):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_with_dep1():
        return 83

    class V2002(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).existed]

    versions = VersionBundle(
        Version(date(2002, 1, 1), V2002),
        Version(date(2001, 1, 1)),
        Version(date(2000, 1, 1)),
    )

    routers = router.create_versioned_copies(versions, latest_schemas_module=None)

    assert client(routers[date(2002, 1, 1)]).get("/test").json() == {
        "detail": "Not Found",
    }

    assert client(routers[date(2001, 1, 1)]).get("/test").json() == 83

    assert client(routers[date(2000, 1, 1)]).get("/test").json() == 83


def test__cascading_router_didnt_exist(router: VersionedAPIRouter):
    @router.get("/test")
    async def test_with_dep1():
        return 83

    class V2002(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [
            endpoint("/test", ["GET"]).didnt_exist,
        ]

    versions = VersionBundle(
        Version(date(2002, 1, 1), V2002),
        Version(date(2001, 1, 1)),
        Version(date(2000, 1, 1)),
    )

    routers = router.create_versioned_copies(versions, latest_schemas_module=None)

    assert client(routers[date(2002, 1, 1)]).get("/test").json() == 83

    assert client(routers[date(2001, 1, 1)]).get("/test").json() == {
        "detail": "Not Found",
    }

    assert client(routers[date(2000, 1, 1)]).get("/test").json() == {
        "detail": "Not Found",
    }
