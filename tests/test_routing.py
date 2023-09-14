import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import date
from typing import Annotated, Any, NewType, TypeAlias, cast, get_args

import pytest
from fastapi import APIRouter, Body, Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pytest_fixture_classes import fixture_class
from starlette.responses import FileResponse

from tests._data import latest
from tests._data.latest import some_schema
from tests._data.unversioned_schema_dir import UnversionedSchema2
from tests._data.unversioned_schema_dir.unversioned_schemas import UnversionedSchema1
from tests._data.unversioned_schemas import UnversionedSchema3
from tests.conftest import GenerateTestVersionPackages
from universi import VersionBundle, VersionedAPIRouter
from universi.exceptions import RouterGenerationError
from universi.routing import generate_all_router_versions
from universi.structure import Version, endpoint, schema
from universi.structure.endpoints import AlterEndpointSubInstruction
from universi.structure.enums import AlterEnumSubInstruction, enum
from universi.structure.schemas import AlterSchemaSubInstruction
from universi.structure.versions import VersionChange

Default = object()
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


@fixture_class(name="create_versioned_copies")
class CreateVersionedCopies:
    api_version_var: ContextVar[date | None]
    router: VersionedAPIRouter

    def __call__(
        self,
        *instructions: AlterSchemaSubInstruction | AlterEndpointSubInstruction | AlterEnumSubInstruction,
        latest_schemas_module: Any = Default,
    ) -> dict[date, APIRouter]:
        class MyVersionChange(VersionChange):
            description = "..."
            instructions_to_migrate_to_previous_version = instructions

        if latest_schemas_module is Default:
            latest_schemas_module = None

        return generate_all_router_versions(
            self.router,
            versions=VersionBundle(
                Version(date(2001, 1, 1), MyVersionChange),
                Version(date(2000, 1, 1)),
                api_version_var=self.api_version_var,
            ),
            latest_schemas_module=latest_schemas_module,
        )


@fixture_class(name="create_versioned_api_routes")
class CreateVersionedAPIRoutes:
    create_versioned_copies: CreateVersionedCopies

    def __call__(
        self,
        *instructions: AlterSchemaSubInstruction | AlterEndpointSubInstruction | AlterEnumSubInstruction,
        latest_schemas_module: Any = Default,
    ) -> tuple[list[APIRoute], list[APIRoute]]:
        routers = self.create_versioned_copies(*instructions, latest_schemas_module=latest_schemas_module)
        for router in routers.values():
            for route in router.routes:
                assert isinstance(route, APIRoute)
        return cast(
            tuple[list[APIRoute], list[APIRoute]],
            (routers[date(2000, 1, 1)].routes, routers[date(2001, 1, 1)].routes),
        )


def test__router_generation__forgot_to_generate_schemas__error(
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    with pytest.raises(
        RouterGenerationError,
        match="Versioned schema directory '.+' does not exist.",
    ):
        create_versioned_api_routes(latest_schemas_module=latest)


def test__endpoint_didnt_exist(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    routes_2000, routes_2001 = create_versioned_api_routes(
        endpoint(test_path, ["GET"]).didnt_exist,
    )

    assert routes_2000 == []
    assert len(routes_2001) == 1
    assert routes_2001[0].endpoint.func == test_endpoint


def test__endpoint_existed(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    @router.post("/test")
    async def test_endpoint_post():
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        endpoint("/test", ["GET"]).existed,
    )

    assert len(routes_2000) == 2
    assert routes_2000[0].endpoint.func == test_endpoint
    assert routes_2000[1].endpoint.func == test_endpoint_post

    assert len(routes_2001) == 1
    assert routes_2001[0].endpoint.func == test_endpoint_post


def test__endpoint_existed__endpoint_removed_in_latest_but_never_restored__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    # with insert_pytest_raises():
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Every route you mark with @VersionedAPIRouter.only_exists_in_older_versions must be restored in one "
            "of the older versions. Otherwise you just need to delete it altogether. The following routes have been "
            "marked with that decorator but were never restored: "
            "[APIRoute(path='/test', name='test_endpoint', methods=['GET'])]",
        ),
    ):
        create_versioned_api_routes()


def test__endpoint_existed__deleting_restoring_deleting_restoring_an_endpoint(
    router: VersionedAPIRouter,
    api_version_var: ContextVar[date | None],
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_endpoint():
        raise NotImplementedError

    class MyVersionChange3(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).existed]

    class MyVersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).didnt_exist]

    class MyVersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [endpoint("/test", ["GET"]).existed]

    routers = generate_all_router_versions(
        router,
        versions=VersionBundle(
            Version(date(2003, 1, 1), MyVersionChange3),
            Version(date(2002, 1, 1), MyVersionChange2),
            Version(date(2001, 1, 1), MyVersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
        latest_schemas_module=None,
    )

    assert len(routers[date(2003, 1, 1)].routes) == 0
    assert len(routers[date(2002, 1, 1)].routes) == 1
    assert len(routers[date(2001, 1, 1)].routes) == 0
    assert len(routers[date(2000, 1, 1)].routes) == 1


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
    attr: str,
    attr_value: Any,
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    routes_2000, routes_2001 = create_versioned_api_routes(
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
        match=re.escape(
            'Route not found on endpoint: "test2". Are you sure it\'s a route and decorators are in the correct order?'
        ),
    ):

        @router.only_exists_in_older_versions
        async def test2():
            raise NotImplementedError


def test__router_generation__creating_a_synchronous_endpoint__error(
    router: VersionedAPIRouter,
    create_versioned_copies: CreateVersionedCopies,
):
    @router.get("/test")
    def test():
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape("All versioned endpoints must be asynchronous."),
    ):
        create_versioned_copies(endpoint("/test", ["GET"]).didnt_exist)


def test__router_generation__changing_a_deleted_endpoint__error(
    router: VersionedAPIRouter,
    create_versioned_copies: CreateVersionedCopies,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test():
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to change in "MyVersionChange" doesn\'t exist',
        ),
    ):
        create_versioned_copies(endpoint("/test", ["GET"]).had(description="Hewwo"))


def test__router_generation__re_creating_an_existing_endpoint__error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_copies: CreateVersionedCopies,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            "Endpoint \"['GET'] /test/{hewoo}\" you tried to restore in "
            '"MyVersionChange" already existed in a newer version',
        ),
    ):
        create_versioned_copies(endpoint(test_path, ["GET"]).existed)


def test__router_generation__editing_an_endpoint_with_wrong_method__should_raise_error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_copies: CreateVersionedCopies,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape('Endpoint "[\'POST\'] /test/{hewoo}" you tried to change in "MyVersionChange" doesn\'t exist'),
    ):
        create_versioned_copies(endpoint(test_path, ["POST"]).had(description="Hewwo"))


def test__router_generation__editing_an_endpoint_with_a_less_general_method__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_copies: CreateVersionedCopies,
):
    @router.route("/test/{hewoo}", methods=["GET", "POST"])
    async def test(hewwo: int):
        raise NotImplementedError

    with pytest.raises(
        RouterGenerationError,
        match=re.escape('Endpoint "[\'GET\'] /test/{hewoo}" you tried to change in "MyVersionChange" doesn\'t exist'),
    ):
        create_versioned_copies(endpoint("/test/{hewoo}", ["GET"]).had(description="Hewwo"))


def test__router_generation__editing_multiple_endpoints_with_same_route(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.api_route("/test/{hewoo}", methods=["GET", "POST"])
    async def test(hewwo: int):
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        endpoint("/test/{hewoo}", ["GET", "POST"]).had(description="Meaw"),
    )
    assert len(routes_2000) == len(routes_2001) == 1
    assert routes_2000[0].description == "Meaw"
    assert routes_2001[0].description == ""


def test__router_generation__editing_an_endpoint_with_a_more_general_method__should_raise_error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_copies: CreateVersionedCopies,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape('Endpoint "[\'POST\'] /test/{hewoo}" you tried to change in "MyVersionChange" doesn\'t exist'),
    ):
        create_versioned_copies(endpoint(test_path, ["GET", "POST"]).had(description="Hewwo"))


def test__router_generation__editing_multiple_methods_of_multiple_endpoints__should_edit_both_methods(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.get("/test")
    async def test_get():
        raise NotImplementedError

    @router.post("/test")
    async def test_post():
        raise NotImplementedError

    routes_2000, routes_2001 = create_versioned_api_routes(
        endpoint("/test", ["GET", "POST"]).had(description="Meaw"),
    )
    assert routes_2000[0].description == "Meaw"
    assert routes_2000[1].description == "Meaw"

    assert routes_2001[0].description == ""
    assert routes_2001[1].description == ""


def test__router_generation__deleting_a_deleted_endpoint__error(
    router: VersionedAPIRouter,
    create_versioned_copies: CreateVersionedCopies,
):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test():
        raise NotImplementedError

    # with insert_pytest_raises():
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to delete in "MyVersionChange" was already deleted in a '
            "newer version. If you really have two routes with the same paths and methods, please, use "
            '"endpoint(..., func_name=...)" to distinguish between them. '
            "Function names of endpoints that were already deleted: ['test']",
        ),
    ):
        create_versioned_copies(endpoint("/test", ["GET"]).didnt_exist)


@pytest.mark.parametrize("delete_first", [True, False])
@pytest.mark.parametrize("route_index_to_delete_first", [0, 1])
def test__router_generation__restoring_deleted_routes_for_same_path_with_func_name__should_restore_only_one_route(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
    route_index_to_delete_first: int,
    delete_first: bool,
):
    @router.get("/test")
    async def test_get0():
        raise NotImplementedError

    @router.get("/test")
    async def test_get1():
        raise NotImplementedError

    routes = [test_get0, test_get1]
    route_to_delete_first = routes[route_index_to_delete_first]
    route_to_delete_second = routes[route_index_to_delete_first - 1]
    router.only_exists_in_older_versions(route_to_delete_first)
    instructions = [
        endpoint("/test", ["GET"], func_name=route_to_delete_first.__name__).existed,
        endpoint("/test", ["GET"], func_name=route_to_delete_second.__name__).didnt_exist,
    ]
    if delete_first:
        instructions = reversed(instructions)

    routes_2000, routes_2001 = create_versioned_api_routes(*instructions)

    assert len(routes_2000) == len(routes_2001) == 1
    assert routes_2000[0].endpoint.func == routes[route_index_to_delete_first]
    assert routes_2001[0].endpoint.func == routes[route_index_to_delete_first - 1]


@pytest.mark.parametrize("route_index_to_delete_first", [0, 1])
def test__router_generation__restoring_deleted_route_for_same_path_without_func_name__should_raise_error(
    router: VersionedAPIRouter,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
    route_index_to_delete_first: int,
):
    @router.get("/test")
    async def test_get0():
        raise NotImplementedError

    @router.get("/test")
    async def test_get1():
        raise NotImplementedError

    routes = [test_get0, test_get1]

    router.only_exists_in_older_versions(routes[route_index_to_delete_first])

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to delete in "MyVersionChange" was already deleted in '
            "a newer version. If you really have two routes with the same paths and methods, please, use "
            '"endpoint(..., func_name=...)" to distinguish between them. '
            f"Function names of endpoints that were already deleted: ['test_get{route_index_to_delete_first}']",
        ),
    ):
        create_versioned_api_routes(
            endpoint("/test", ["GET"]).didnt_exist,
            endpoint("/test", ["GET"]).existed,
        )


@pytest.fixture()
def two_deleted_routes(router: VersionedAPIRouter):
    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_get0():
        raise NotImplementedError

    @router.only_exists_in_older_versions
    @router.get("/test")
    async def test_get1():
        raise NotImplementedError

    return test_get0, test_get1


def test__router_generation__restoring_two_deleted_routes_for_same_path__should_raise_error(
    two_deleted_routes: tuple[Endpoint, Endpoint],
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to restore in "MyVersionChange" has different applicable routes that '
            "could be restored. If you really have two routes with the same paths and methods, please, use "
            '"endpoint(..., func_name=...)" to distinguish between them. '
            "Function names of endpoints that can be restored: ['test_get1', 'test_get0']",
        ),
    ):
        create_versioned_api_routes(endpoint("/test", ["GET"]).existed)


@pytest.mark.parametrize("route_index_to_restore_first", [0, 1])
def test__endpoint_existed__deleting_and_restoring_two_routes_for_the_same_endpoint(
    router: VersionedAPIRouter,
    api_version_var: ContextVar[date | None],
    two_deleted_routes: tuple[Endpoint, Endpoint],
    route_index_to_restore_first: int,
):
    route_to_restore_first = two_deleted_routes[route_index_to_restore_first]
    route_to_restore_second = two_deleted_routes[route_index_to_restore_first - 1]

    class MyVersionChange2(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            endpoint("/test", ["GET"], func_name=route_to_restore_first.__name__).existed,
        ]

    class MyVersionChange1(VersionChange):
        description = "..."
        instructions_to_migrate_to_previous_version = [
            endpoint("/test", ["GET"], func_name=route_to_restore_second.__name__).existed,
        ]

    routers = generate_all_router_versions(
        router,
        versions=VersionBundle(
            Version(date(2002, 1, 1), MyVersionChange2),
            Version(date(2001, 1, 1), MyVersionChange1),
            Version(date(2000, 1, 1)),
            api_version_var=api_version_var,
        ),
        latest_schemas_module=None,
    )

    assert len(routers[date(2002, 1, 1)].routes) == 0
    assert len(routers[date(2001, 1, 1)].routes) == 1
    assert len(routers[date(2000, 1, 1)].routes) == 2

    assert routers[date(2001, 1, 1)].routes[0].endpoint.func == route_to_restore_first
    assert {routers[date(2000, 1, 1)].routes[0].endpoint.func, routers[date(2000, 1, 1)].routes[1].endpoint.func} == {
        route_to_restore_first,
        route_to_restore_second,
    }


def get_nested_field_type(annotation: Any) -> type[BaseModel]:
    return get_args(get_args(annotation)[1])[0].__fields__["foo"].type_.__fields__["foo"].annotation


def test__router_generation__re_creating_a_non_endpoint__error(
    create_versioned_copies: CreateVersionedCopies,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Endpoint "[\'GET\'] /test" you tried to restore in "MyVersionChange" wasn\'t among the deleted routes',
        ),
    ):
        create_versioned_copies(endpoint("/test", ["GET"]).existed)


def test__router_generation__changing_attribute_to_the_same_value__error(
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_copies: CreateVersionedCopies,
):
    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'Expected attribute "path" of endpoint "[\'GET\'] /test/{hewoo}" to be different in "MyVersionChange", but '
            "it was the same. It means that your version change has no effect on the attribute and can be removed.",
        ),
    ):
        create_versioned_copies(endpoint(test_path, ["GET"]).had(path=test_path))


def test__router_generation__non_api_route_added(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
    create_versioned_copies: CreateVersionedCopies,
):
    @router.websocket("/test2")
    async def test_websocket():
        raise NotImplementedError

    routers = create_versioned_copies(endpoint(test_path, ["GET"]).didnt_exist)
    assert len(routers[date(2000, 1, 1)].routes) == 1
    assert len(routers[date(2001, 1, 1)].routes) == 2
    route = routers[date(2001, 1, 1)].routes[0]
    assert isinstance(route, APIRoute)
    assert route.endpoint.func == test_endpoint


def test__router_generation__non_api_route_added_with_schemas(
    router: VersionedAPIRouter,
    test_endpoint: Endpoint,
    test_path: str,
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_copies: CreateVersionedCopies,
):
    @router.websocket("/test2")
    async def test_websocket():
        raise NotImplementedError

    generate_test_version_packages()
    routers = create_versioned_copies(endpoint(test_path, ["GET"]).didnt_exist, latest_schemas_module=latest)
    assert len(routers[date(2000, 1, 1)].routes) == 1
    assert len(routers[date(2001, 1, 1)].routes) == 2
    route = routers[date(2001, 1, 1)].routes[0]
    assert isinstance(route, APIRoute)
    assert route.endpoint.func == test_endpoint


def test__router_generation__updating_response_model_when_schema_is_defined_in_a_non_init_file(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.get("/test", response_model=some_schema.MySchema)
    async def test():
        raise NotImplementedError

    instruction = schema(some_schema.MySchema).field("foo").had(type=str)
    generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(instruction, latest_schemas_module=latest)
    assert routes_2000[0].response_model.__fields__["foo"].annotation == str
    assert routes_2001[0].response_model.__fields__["foo"].annotation == int


def test__router_generation__updating_response_model(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.get(
        "/test",
        response_model=dict[str, list[latest.SchemaWithOnePydanticField]],
    )
    async def test():
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    schemas_2000, schemas_2001 = generate_test_version_packages(instruction)
    routes_2000, routes_2001 = create_versioned_api_routes(instruction, latest_schemas_module=latest)

    assert len(routes_2000) == len(routes_2001) == 1
    assert routes_2000[0].response_model == dict[str, list[schemas_2000.SchemaWithOnePydanticField]]
    assert routes_2001[0].response_model == dict[str, list[schemas_2001.SchemaWithOnePydanticField]]

    assert get_nested_field_type(routes_2000[0].response_model) == list[str]
    assert get_nested_field_type(routes_2001[0].response_model) == int


@pytest.mark.parametrize("schemas_to_pick", [0, 1])
def test__router_generation__using_non_latest_version_of_schema__should_raise_error(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
    schemas_to_pick: int,
):
    schemas = generate_test_version_packages()

    @router.post("/testik")
    async def testik(body: schemas[schemas_to_pick].SchemaWithOnePydanticField):
        raise NotImplementedError

    # with insert_pytest_raises():
    with pytest.raises(
        RouterGenerationError,
        match=f"\"<class \\'tests\\._data\\.v200{schemas_to_pick}_01_01\\.SchemaWithOnePydanticField\\'>\" "
        f'is not defined in ".+latest" even though it must be\\. It is defined in ".+v200{schemas_to_pick}_01_01"\\. '
        "It probably means that you used a specific version of the class in "
        'fastapi dependencies or pydantic schemas instead of "latest"\\.',
    ):
        create_versioned_api_routes(latest_schemas_module=latest)


def test__router_generation__passing_a_module_instead_of_package_for_latest__should_raise_error(
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    from tests._data.latest import weird_schemas

    with pytest.raises(
        RouterGenerationError,
        match=re.escape(
            'The latest schemas module must be a package. "tests._data.latest.weird_schemas" is not a package.',
        ),
    ):
        create_versioned_api_routes(latest_schemas_module=weird_schemas)


def test__router_generation__passing_a_package_with_wrong_name_instead_of_latest__should_raise_error(
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    from tests import _data

    with pytest.raises(
        RouterGenerationError,
        match=re.escape('The name of the latest schemas module must be "latest". Received "tests._data" instead.'),
    ):
        create_versioned_api_routes(latest_schemas_module=_data)


def test__router_generation__updating_request_models(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    @router.get("/test")
    async def test(body: dict[str, list[latest.SchemaWithOnePydanticField]]):
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    schemas_2000, schemas_2001 = generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(instruction, latest_schemas_module=latest)
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
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
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

    routes_2000, routes_2001 = create_versioned_api_routes(instruction, latest_schemas_module=latest)

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
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_api_routes: CreateVersionedAPIRoutes,
):
    newtype = NewType("newtype", str)

    @router.get("/test")
    async def test(param1: newtype = Body(), param2: str | int = Body()):  # noqa: B008
        raise NotImplementedError

    instruction = schema(latest.SchemaWithOneIntField).field("foo").had(type=list[str])
    generate_test_version_packages(instruction)

    routes_2000, routes_2001 = create_versioned_api_routes(instruction, latest_schemas_module=latest)
    assert len(routes_2000) == len(routes_2001) == 1

    assert routes_2000[0].dependant.body_params[0].annotation is newtype
    assert routes_2001[0].dependant.body_params[0].annotation is newtype

    assert routes_2000[0].dependant.body_params[1].annotation == str | int
    assert routes_2001[0].dependant.body_params[1].annotation == str | int


def test__router_generation__updating_request_depends(
    router: VersionedAPIRouter,
    _reload_autogenerated_modules: None,
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_copies: CreateVersionedCopies,
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

    routers = create_versioned_copies(instruction, latest_schemas_module=latest)
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
    generate_test_version_packages: GenerateTestVersionPackages,
    create_versioned_copies: CreateVersionedCopies,
):
    def dependency(my_enum: latest.StrEnum):
        return my_enum

    @router.get("/test", dependencies=[Depends(dependency)])
    async def test_with_dep():
        pass

    instruction = enum(latest.StrEnum).had(foo="bar")
    generate_test_version_packages(instruction)
    routers = create_versioned_copies(instruction, latest_schemas_module=latest)

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


def test__cascading_router_exists(router: VersionedAPIRouter, api_version_var: ContextVar[date | None]):
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
        api_version_var=api_version_var,
    )
    routers = generate_all_router_versions(router, versions=versions, latest_schemas_module=None)

    assert client(routers[date(2002, 1, 1)]).get("/test").json() == {
        "detail": "Not Found",
    }

    assert client(routers[date(2001, 1, 1)]).get("/test").json() == 83

    assert client(routers[date(2000, 1, 1)]).get("/test").json() == 83


def test__cascading_router_didnt_exist(router: VersionedAPIRouter, api_version_var: ContextVar[date | None]):
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
        api_version_var=api_version_var,
    )

    routers = generate_all_router_versions(router, versions=versions, latest_schemas_module=None)

    assert client(routers[date(2002, 1, 1)]).get("/test").json() == 83

    assert client(routers[date(2001, 1, 1)]).get("/test").json() == {
        "detail": "Not Found",
    }

    assert client(routers[date(2000, 1, 1)]).get("/test").json() == {
        "detail": "Not Found",
    }


def test__generate_all_router_versions__two_routers(
    router: VersionedAPIRouter, test_endpoint: Endpoint, test_path: str, api_version_var: ContextVar[date | None]
):
    router2 = VersionedAPIRouter(prefix="/api2")

    @router2.get("/test")
    async def test_endpoint2():
        raise NotImplementedError

    class V2001(VersionChange):
        description = ""
        instructions_to_migrate_to_previous_version = [
            endpoint(test_path, ["GET"]).didnt_exist,
            endpoint("/api2/test", ["GET"]).had(description="Meaw"),
        ]

    versions = VersionBundle(
        Version(date(2001, 1, 1), V2001),
        Version(date(2000, 1, 1)),
        api_version_var=api_version_var,
    )

    routers = generate_all_router_versions(router, router2, versions=versions, latest_schemas_module=None)
    assert len(routers[date(2001, 1, 1)].routes) == 2
    assert len(routers[date(2000, 1, 1)].routes) == 1
    assert {routers[date(2001, 1, 1)].routes[0].endpoint.func, routers[date(2001, 1, 1)].routes[1].endpoint.func} == {
        test_endpoint,
        test_endpoint2,
    }
    assert routers[date(2000, 1, 1)].routes[0].endpoint.func == test_endpoint2
    assert routers[date(2000, 1, 1)].routes[0].endpoint.func == test_endpoint2
