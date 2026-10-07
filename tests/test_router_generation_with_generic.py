from typing import Annotated, Any, Generic, TypeVar, Union

import pytest
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_422_UNPROCESSABLE_CONTENT

from cadwyn import RequestInfo, convert_request_to_next_version_for
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import VersionChange
from tests.conftest import CreateVersionedApp, version_change

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


ItemT = TypeVar("ItemT")


class Item(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    full_name: str = Field(alias="fullName")


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]


class PageWithTotal(Page[ItemT], Generic[ItemT]):
    total: int


class ItemPage(Page[Item]):
    total: int


def item_alias_version_change() -> type[VersionChange]:
    return version_change(
        schema(Item)
        .field("full_name")
        .had(
            alias="full_name",
            validation_alias=AliasChoices("full_name", "fullName"),
            serialization_alias="full_name",
        ),
    )


@pytest.mark.parametrize("page_model", [Page[Item], PageWithTotal[Item], ItemPage])
def test__router_generation__generic_response_model_with_versioned_arg__should_use_arg_from_requested_version(
    page_model: type[Page[Item]],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=page_model)
    async def list_items() -> dict[str, Any]:
        return {"items": [{"fullName": "Ada"}], "total": 1}

    app = create_versioned_app(item_alias_version_change(), router=router)
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/items").json()["items"] == [{"full_name": "Ada"}]
    assert client_2001.get("/items").json()["items"] == [{"fullName": "Ada"}]

    old_schemas = unversioned_client.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert old_schemas["Item"]["properties"] == {"full_name": {"type": "string", "title": "Full Name"}}
    assert old_schemas[page_model.__name__.replace("[", "_").replace("]", "_")]["properties"]["items"] == {
        "items": {"$ref": "#/components/schemas/Item"},
        "type": "array",
        "title": "Items",
    }


def test__router_generation__generic_request_body_with_versioned_arg__should_use_arg_from_requested_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/items")
    async def create_items(page: Page[Item]) -> list[str]:
        return [item.full_name for item in page.items]

    app = create_versioned_app(item_alias_version_change(), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.post("/items", json={"items": [{"full_name": "Ada"}]}).json() == ["Ada"]
    assert client_2001.post("/items", json={"items": [{"fullName": "Ada"}]}).json() == ["Ada"]
