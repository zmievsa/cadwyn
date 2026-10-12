from typing import Annotated, Any, Generic, Optional, TypeVar, Union

import pytest
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, Field, create_model
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


class Category(BaseModel):
    name: str = Field(alias="fullName")
    subcategories: Optional[Page["Category"]] = None


class Ref(BaseModel, Generic[ItemT]):
    id: int


class User(BaseModel):
    name: str = Field(alias="fullName")
    parent: Optional[Ref["User"]] = None
    friends: "list[User]" = []


class Node(BaseModel):
    name: str = Field(alias="fullName")
    children: "Page[Node]"


class A(BaseModel):
    name: str = Field(alias="fullName")
    b: "Optional[Page[B]]" = None


class B(BaseModel):
    name: str = Field(alias="fullName")
    a: "Optional[A]" = None


class TreeNode(BaseModel):
    name: str = Field(alias="fullName")
    leaves: "list[Leaf]" = []


class Leaf(TreeNode):
    weight: int = 0


def old_name_alias(model: type[BaseModel]):
    return (
        schema(model)
        .field("name")
        .had(
            alias="oldName",
            validation_alias=AliasChoices("oldName", "fullName"),
            serialization_alias="oldName",
        )
    )


def item_alias_version_change(**body_items: Any) -> type[VersionChange]:
    return version_change(old_name_alias(Item), **body_items)


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

    app = create_versioned_app(version_change(old_name_alias(LocalItem)), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/items").json() == {"items": [{"oldName": "Ada"}]}
    assert client_2001.get("/items").json() == {"items": [{"fullName": "Ada"}]}


def test__router_generation__generic_from_unimportable_module__should_use_arg_from_requested_version(
    create_versioned_app: CreateVersionedApp,
):
    class LocalItem(BaseModel):
        name: str = Field(alias="fullName")

    local_page = create_model(  # ty: ignore[no-matching-overload]
        "LocalPage",
        __base__=(BaseModel, Generic[ItemT]),
        __module__="unimportable_module",
        items=(list[ItemT], ...),
    )
    router = VersionedAPIRouter()

    @router.get("/items", response_model=local_page[LocalItem])
    async def list_items() -> dict[str, Any]:
        return {"items": [{"fullName": "Ada"}]}

    app = create_versioned_app(version_change(old_name_alias(LocalItem)), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/items").json() == {"items": [{"oldName": "Ada"}]}
    assert client_2001.get("/items").json() == {"items": [{"fullName": "Ada"}]}


@pytest.mark.parametrize(
    ("response_model", "versioned_models", "latest_body", "old_body", "old_schema_names"),
    [
        pytest.param(
            Category,
            [Category],
            {"fullName": "a", "subcategories": {"items": [{"fullName": "b"}]}},
            {"oldName": "a", "subcategories": {"items": [{"oldName": "b", "subcategories": None}]}},
            {"Category", "Page_Category_"},
            id="quoted_generic_arg",
        ),
        pytest.param(
            User,
            [User],
            {"fullName": "a", "parent": {"id": 1}, "friends": [{"fullName": "b"}]},
            {"oldName": "a", "parent": {"id": 1}, "friends": [{"oldName": "b", "parent": None, "friends": []}]},
            {"User", "Ref_User_"},
            id="generic_arg_unused_in_fields",
        ),
        pytest.param(
            Node,
            [Node],
            {"fullName": "a", "children": {"items": [{"fullName": "b", "children": {"items": []}}]}},
            {"oldName": "a", "children": {"items": [{"oldName": "b", "children": {"items": []}}]}},
            {"Node", "Page_Node_"},
            id="quoted_generic",
        ),
        pytest.param(
            A,
            [A, B],
            {"fullName": "a", "b": {"items": [{"fullName": "b", "a": {"fullName": "c"}}]}},
            {"oldName": "a", "b": {"items": [{"oldName": "b", "a": {"oldName": "c", "b": None}}]}},
            {"A", "B", "Page_B_"},
            id="mutual_reference_through_generic",
        ),
        pytest.param(
            TreeNode,
            [TreeNode],
            {"fullName": "a", "leaves": [{"fullName": "b", "weight": 1}]},
            {"oldName": "a", "leaves": [{"oldName": "b", "leaves": [], "weight": 1}]},
            {"TreeNode", "Leaf"},
            id="reference_to_subclass",
        ),
    ],
)
def test__router_generation__self_referencing_models__should_use_models_from_requested_version(
    response_model: type[BaseModel],
    versioned_models: list[type[BaseModel]],
    latest_body: dict[str, Any],
    old_body: dict[str, Any],
    old_schema_names: set[str],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/models", response_model=response_model)
    async def get_model() -> dict[str, Any]:
        return latest_body

    app = create_versioned_app(version_change(*(old_name_alias(model) for model in versioned_models)), router=router)
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/models").json() == old_body
    assert client_2001.get("/models").status_code == HTTP_200_OK
    old_openapi = unversioned_client.get("/openapi.json?version=2000-01-01").json()
    assert set(old_openapi["components"]["schemas"]) == old_schema_names | {"HTTPValidationError", "ValidationError"}
