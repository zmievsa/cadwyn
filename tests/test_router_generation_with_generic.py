from typing import Annotated, Any, Generic, TypeVar, Union

import pytest
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, Field
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
    name: str = Field(alias="fullName")


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]


class PageWithTotal(Page[ItemT], Generic[ItemT]):
    total: int


class ItemPage(Page[Item]):
    total: int


def item_alias_version_change(**body_items: Any) -> type[VersionChange]:
    return version_change(
        schema(Item)
        .field("name")
        .had(
            alias="oldName",
            validation_alias=AliasChoices("oldName", "fullName"),
            serialization_alias="oldName",
        ),
        **body_items,
    )


def get_response_schema(openapi: dict[str, Any], path: str) -> dict[str, Any]:
    ref = openapi["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    return openapi["components"]["schemas"][ref.removeprefix("#/components/schemas/")]


@pytest.mark.parametrize(
    "page_model",
    [Page[Item], Page["Item"], PageWithTotal[Item], ItemPage],
    ids=["generic", "generic_with_quoted_arg", "generic_subclass", "parametrized_subclass"],
)
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

    assert client_2000.get("/items").json()["items"] == [{"oldName": "Ada"}]
    assert client_2001.get("/items").json()["items"] == [{"fullName": "Ada"}]

    old_openapi = unversioned_client.get("/openapi.json?version=2000-01-01").json()
    assert get_response_schema(old_openapi, "/items")["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/Item"
    }
    assert old_openapi["components"]["schemas"]["Item"]["properties"] == {
        "oldName": {"type": "string", "title": "Oldname"}
    }


@pytest.mark.parametrize("page_model", [Page[Item], Page["Item"]], ids=["generic", "generic_with_quoted_arg"])
def test__router_generation__generic_request_body_with_versioned_arg__should_use_arg_from_requested_version(
    page_model: type[Page[Item]],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/items")
    async def create_items(page: page_model) -> list[str]:  # ty: ignore[invalid-type-form]
        return [item.name for item in page.items]

    @convert_request_to_next_version_for(page_model)
    def rename_old_name_to_full_name(request: RequestInfo) -> None:
        for item in request.body["items"]:
            item["fullName"] = item.pop("oldName")

    app = create_versioned_app(
        item_alias_version_change(rename_old_name_to_full_name=rename_old_name_to_full_name), router=router
    )
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.post("/items", json={"items": [{"oldName": "Ada"}]}).json() == ["Ada"]
    assert client_2001.post("/items", json={"items": [{"fullName": "Ada"}]}).json() == ["Ada"]


def test__router_generation__local_generic_with_quoted_arg__should_use_arg_from_requested_version(
    create_versioned_app: CreateVersionedApp,
):
    class LocalItem(BaseModel):
        name: str = Field(alias="fullName")

    class LocalPage(BaseModel, Generic[ItemT]):
        items: list[ItemT]

    router = VersionedAPIRouter()

    @router.get("/items", response_model=LocalPage["LocalItem"])
    async def list_items() -> dict[str, Any]:
        return {"items": [{"fullName": "Ada"}]}

    app = create_versioned_app(
        version_change(
            schema(LocalItem)
            .field("name")
            .had(
                alias="oldName",
                validation_alias=AliasChoices("oldName", "fullName"),
                serialization_alias="oldName",
            )
        ),
        router=router,
    )
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/items").json() == {"items": [{"oldName": "Ada"}]}
    assert client_2001.get("/items").json() == {"items": [{"fullName": "Ada"}]}
