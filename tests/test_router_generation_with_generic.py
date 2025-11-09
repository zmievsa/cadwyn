from typing import Annotated, Generic, TypeVar, Union

from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_422_UNPROCESSABLE_CONTENT

from cadwyn import RequestInfo, convert_request_to_next_version_for
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import VersionChange
from tests.conftest import CreateVersionedApp

BoolT = TypeVar("BoolT", bound=Union[bool, int])


class GenericSchema(BaseModel, Generic[BoolT]):
    foo: Annotated[bool, Field(title="Foo", strict=True)]
    bar: Annotated[BoolT, Field(title="Bar")]


class ParametrizedSchema(GenericSchema[bool]):
    pass


class GenericVersionChange(VersionChange):
    description = "Generic"
    instructions_to_migrate_to_previous_version = (
        schema(GenericSchema).field("foo").didnt_have("strict"),
        schema(GenericSchema).field("foo").had(type=BoolT),
        schema(GenericSchema).field("bar").had(description="A bar"),
        schema(GenericSchema).field("bar").didnt_have("title"),
    )

    @convert_request_to_next_version_for(GenericSchema)
    def to_bool(request: RequestInfo) -> None:
        request.body["foo"] = bool(request.body["foo"])


class ParametrizedVersionChange(VersionChange):
    description = "Parametrized"
    instructions_to_migrate_to_previous_version = (
        schema(ParametrizedSchema).field("bar").had(description="A bar"),
        schema(ParametrizedSchema).field("bar").didnt_have("title"),
    )


router = VersionedAPIRouter()


@router.post("/generic", status_code=HTTP_204_NO_CONTENT)
async def route_with_generic_schema(dep: GenericSchema) -> None:
    pass


@router.post("/parametrized", status_code=HTTP_204_NO_CONTENT)
async def route_with_parametrized_schema(dep: ParametrizedSchema) -> None:
    pass


def test__router_generation__using_generic_schema_in_body(
    create_versioned_app: CreateVersionedApp,
):
    app = create_versioned_app(
        GenericVersionChange,
        router=router,
    )

    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.post("/generic", json={"foo": 1, "bar": False}).status_code == HTTP_204_NO_CONTENT

    assert client_2001.post("/generic", json={"foo": True, "bar": 0}).status_code == HTTP_204_NO_CONTENT
    assert client_2001.post("/generic", json={"foo": 1, "bar": False}).status_code == HTTP_422_UNPROCESSABLE_CONTENT

    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == HTTP_200_OK


def test__router_generation__using_parametrized_schema_in_body(
    create_versioned_app: CreateVersionedApp,
):
    app = create_versioned_app(
        ParametrizedVersionChange,
        router=router,
    )

    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.post("/parametrized", json={"foo": True, "bar": False}).status_code == HTTP_204_NO_CONTENT
    assert client_2001.post("/parametrized", json={"foo": True, "bar": False}).status_code == HTTP_204_NO_CONTENT

    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == HTTP_200_OK
