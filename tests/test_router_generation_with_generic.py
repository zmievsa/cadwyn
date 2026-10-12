import functools
import typing
from typing import Annotated, Any, Generic, Optional, TypeVar, Union

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, Field, JsonValue, create_model
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_422_UNPROCESSABLE_CONTENT
from typing_extensions import Self, TypeAliasType, TypeVarTuple, Unpack

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
ItemsT = TypeVarTuple("ItemsT")


class Item(BaseModel):
    name: str = Field(alias="fullName")


class Page(BaseModel, Generic[ItemT]):
    items: list[ItemT]


class PageWithTotal(Page[ItemT], Generic[ItemT]):
    total: int


class ItemPage(Page[Item]):
    total: int


class Wrapper(BaseModel, Generic[ItemT]):
    value: ItemT


class WrapperPage(BaseModel, Generic[ItemT]):
    items: list[Wrapper[ItemT]]


class LinkedPage(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    next: Optional["LinkedPage[ItemT]"] = None


class Category(BaseModel):
    name: str = Field(alias="fullName")
    subcategories: Optional[Page["Category"]] = None


class LinkedCategory(BaseModel):
    name: str = Field(alias="fullName")
    subcategories: Optional[LinkedPage["LinkedCategory"]] = None


class Ref(BaseModel, Generic[ItemT]):
    id: int


class User(BaseModel):
    name: str = Field(alias="fullName")
    parent: Optional[Ref["User"]] = None


class Node(BaseModel):
    name: str = Field(alias="fullName")
    children: "Page['Node']"


class A(BaseModel):
    name: str = Field(alias="fullName")
    b: "Optional[Page[B]]" = None


class B(BaseModel):
    name: str = Field(alias="fullName")
    a: "Optional[A]" = None


class Parent(BaseModel):
    name: str = Field("parent", alias="fullName")
    children: Optional[Page["Child"]] = None


class Child(BaseModel):
    parent: Parent = Field(default_factory=Parent)


class Tree(BaseModel):
    name: str
    children: "list[Tree]" = []


class CallbackNode(BaseModel):
    children: Optional[Page["CallbackNode"]] = None
    # typing.Callable cannot be rebuilt from its origin and args unlike collections.abc.Callable
    callback: typing.Callable = Field(default=lambda: None, exclude=True)


def make_values(*values: Unpack[ItemsT]) -> tuple[Unpack[ItemsT]]:
    return values


class VariadicNode(BaseModel):
    children: Optional[Page["VariadicNode"]] = None
    values: tuple[int, ...] = Field(default_factory=make_values)


class CallableDefault:
    def __call__(self, value: Optional["RichNode"] = None) -> list[str]:
        return []


class RichNode(BaseModel):
    children: Optional[Page["RichNode"]] = None
    siblings: "RichNodes" = []
    extra: JsonValue = None
    tags: list[str] = Field(default_factory=functools.partial(list))
    labels: list[str] = Field(default_factory=CallableDefault())


RichNodes = TypeAliasType("RichNodes", list[RichNode])
RichNode.model_rebuild()


def count_models(*, model: Any) -> int:
    return 0


class PartialA(BaseModel):
    name: str = Field(alias="fullName")
    count: int = Field(default_factory=functools.partial(count_models, model=Page["PartialB"]))


class PartialB(BaseModel):
    name: str = Field(alias="fullName")
    a: Optional[Page[PartialA]] = None


def get_dependency_page() -> Page["DependencyB"]:
    raise NotImplementedError("Only its annotations are used")


def count_dependencies(page: Any = Depends(get_dependency_page)) -> int:
    return 0


class DependencyA(BaseModel):
    name: str = Field(alias="fullName")
    count: int = Field(default_factory=count_dependencies)


class DependencyB(BaseModel):
    name: str = Field(alias="fullName")
    a: Optional[Page[DependencyA]] = None


class PartialDictNode(BaseModel):
    children: Optional[Page["PartialDictNode"]] = None
    metadata: dict[str, int] = Field(default_factory=functools.partial(dict, count=1))


class Author(BaseModel):
    name: str = Field(alias="fullName")
    books: Optional[Page["Book"]] = None


class Book(BaseModel):
    name: str = Field(alias="fullName")
    authors: Optional[Page[Author]] = None


Author.model_rebuild()


class SelfReferencingNode(BaseModel):
    value: int
    children: list[Self] = []


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


def test__router_generation__generic_using_other_generic_with_its_type_param__should_use_arg_from_requested_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=WrapperPage[Item])
    async def list_items() -> dict[str, Any]:
        return {"items": [{"value": {"fullName": "Ada"}}]}

    app = create_versioned_app(item_alias_version_change(), router=router)
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/items").json() == {"items": [{"value": {"oldName": "Ada"}}]}
    assert client_2001.get("/items").json() == {"items": [{"value": {"fullName": "Ada"}}]}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK


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


# Type args that refer back to the model being generated cannot use its versioned copy yet so they stay head models
@pytest.mark.parametrize(
    ("response_model", "versioned_models", "body", "old_body"),
    [
        pytest.param(
            Category,
            [Category],
            {"fullName": "a", "subcategories": {"items": [{"fullName": "b"}]}},
            {"oldName": "a", "subcategories": {"items": [{"fullName": "b", "subcategories": None}]}},
            id="quoted_arg",
        ),
        pytest.param(
            LinkedCategory,
            [LinkedCategory],
            {"fullName": "a", "subcategories": {"items": [{"fullName": "b"}], "next": {"items": []}}},
            {
                "oldName": "a",
                "subcategories": {
                    "items": [{"fullName": "b", "subcategories": None}],
                    "next": {"items": [], "next": None},
                },
            },
            id="quoted_arg_of_self_referencing_generic",
        ),
        pytest.param(
            User,
            [User],
            {"fullName": "a", "parent": {"id": 1}},
            {"oldName": "a", "parent": {"id": 1}},
            id="arg_unused_in_fields",
        ),
        pytest.param(
            Node,
            [Node],
            {"fullName": "a", "children": {"items": [{"fullName": "b", "children": {"items": []}}]}},
            {"oldName": "a", "children": {"items": [{"fullName": "b", "children": {"items": []}}]}},
            id="quoted_annotation",
        ),
        pytest.param(
            A,
            [A, B],
            {"fullName": "a", "b": {"items": [{"fullName": "b", "a": {"fullName": "c"}}]}},
            {"oldName": "a", "b": {"items": [{"fullName": "b", "a": {"fullName": "c", "b": None}}]}},
            id="mutual_reference",
        ),
        pytest.param(
            Parent,
            [Parent],
            {"fullName": "a", "children": {"items": [{}]}},
            {"oldName": "a", "children": {"items": [{"parent": {"fullName": "parent", "children": None}}]}},
            id="default_factory",
        ),
        pytest.param(
            Page[Tree],
            [Item],
            {"items": [{"name": "a", "children": [{"name": "b"}]}]},
            {"items": [{"name": "a", "children": [{"name": "b", "children": []}]}]},
            id="arg_referring_to_itself_without_generics",
        ),
    ],
)
def test__router_generation__generic_with_arg_referring_to_model_in_generation__should_fall_back_to_head_arg(
    response_model: type[BaseModel],
    versioned_models: list[type[BaseModel]],
    body: dict[str, Any],
    old_body: dict[str, Any],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/models", response_model=response_model)
    async def get_model() -> dict[str, Any]:
        return body

    app = create_versioned_app(version_change(*(old_name_alias(model) for model in versioned_models)), router=router)
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/models").json() == old_body
    assert client_2001.get("/models").status_code == HTTP_200_OK
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == HTTP_200_OK


def get_old_version_behavior_of_cycle_through_two_generics(
    create_versioned_app: CreateVersionedApp, *, routes_order: list[str], versioned_models: list[type[BaseModel]]
) -> tuple[Any, ...]:
    router = VersionedAPIRouter()

    def get_author() -> dict[str, Any]:
        return {"fullName": "a", "books": {"items": [{"fullName": "b", "authors": {"items": [{"fullName": "c"}]}}]}}

    def get_book() -> dict[str, Any]:
        return {"fullName": "b", "authors": {"items": [{"fullName": "a", "books": {"items": [{"fullName": "c"}]}}]}}

    def create_author(author: Author) -> list[str]:
        return [author.name, *(book.name for book in (author.books.items if author.books else []))]

    routes = {
        "get_author": lambda: router.add_api_route("/authors", get_author, response_model=Author),
        "get_book": lambda: router.add_api_route("/books", get_book, response_model=Book),
        "create_author": lambda: router.add_api_route("/authors", create_author, methods=["POST"]),
    }
    for route in routes_order:
        routes[route]()

    @convert_request_to_next_version_for(Author)
    def rename_old_name_to_full_name(request: RequestInfo) -> None:
        request.body["fullName"] = request.body.pop("oldName")

    app = create_versioned_app(
        version_change(
            *(old_name_alias(model) for model in versioned_models),
            rename_old_name_to_full_name=rename_old_name_to_full_name,
        ),
        router=router,
    )
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    post_response = client_2000.post("/authors", json={"oldName": "a", "books": {"items": [{"fullName": "b"}]}})
    old_openapi = unversioned_client.get("/openapi.json?version=2000-01-01").json()
    return (
        client_2000.get("/authors").json(),
        client_2000.get("/books").json(),
        (post_response.status_code, post_response.json()),
        sorted(old_openapi["components"]["schemas"]),
    )


def test__router_generation__cycle_through_two_generics__should_not_depend_on_route_or_instruction_order(
    create_versioned_app: CreateVersionedApp,
):
    route_orders = (["get_author", "get_book", "create_author"], ["create_author", "get_book", "get_author"])
    instruction_orders: tuple[list[type[BaseModel]], ...] = ([Author, Book], [Book, Author])
    behaviors = [
        get_old_version_behavior_of_cycle_through_two_generics(
            create_versioned_app, routes_order=routes_order, versioned_models=versioned_models
        )
        for routes_order in route_orders
        for versioned_models in instruction_orders
    ]

    old_author, old_book, post_result, old_schema_names = behaviors[0]
    assert all(behavior == behaviors[0] for behavior in behaviors)
    # Both generics in the cycle keep their head args
    assert old_author == {
        "oldName": "a",
        "books": {"items": [{"fullName": "b", "authors": {"items": [{"fullName": "c", "books": None}]}}]},
    }
    assert old_book == {
        "oldName": "b",
        "authors": {"items": [{"fullName": "a", "books": {"items": [{"fullName": "c", "authors": None}]}}]},
    }
    assert post_result == (HTTP_200_OK, ["a", "b"])
    # Same as without versioned type args: one schema per generic and both versions of Author and Book
    module_prefix = "tests__test_router_generation_with_generic__"
    assert old_schema_names == [
        "Book-Input",
        "HTTPValidationError",
        "Page_Author_-Input",
        "Page_Author_-Output",
        "Page_Book_-Input",
        "Page_Book_-Output",
        "ValidationError",
        f"{module_prefix}Author-Input__1",
        f"{module_prefix}Author-Input__2",
        f"{module_prefix}Author-Output__1",
        f"{module_prefix}Author-Output__2",
        f"{module_prefix}Book-Output__1",
        f"{module_prefix}Book__2",
    ]


def test__router_generation__generic_with_arg_using_self__should_generate_versions(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/refs", response_model=Ref[SelfReferencingNode])
    async def get_ref() -> dict[str, Any]:
        return {"id": 1}

    app = create_versioned_app(item_alias_version_change(), router=router)
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert client_2000.get("/refs").json() == {"id": 1}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK


def test__router_generation__model_using_self__should_generate_versions(create_versioned_app: CreateVersionedApp):
    router = VersionedAPIRouter()

    @router.get("/nodes", response_model=SelfReferencingNode)
    async def get_node() -> dict[str, Any]:
        return {"value": 1, "children": [{"value": 2}]}

    app = create_versioned_app(item_alias_version_change(), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    # FastAPI itself cannot render OpenAPI for models that use Self so we only check the response
    assert client_2000.get("/nodes").json() == {"value": 1, "children": [{"value": 2, "children": []}]}


@pytest.mark.parametrize(
    "page_model",
    [
        Page["CallbackNode"],
        Page[CallbackNode],
        Page["VariadicNode"],
        Page[VariadicNode],
        Page["RichNode"],
        Page[RichNode],
        Page["PartialDictNode"],
        Page[PartialDictNode],
    ],
    ids=[
        "cyclic_callable",
        "callable",
        "cyclic_unpack",
        "unpack",
        "cyclic_various",
        "various",
        "cyclic_partial_of_builtin",
        "partial_of_builtin",
    ],
)
def test__router_generation__generic_arg_with_annotations_that_cannot_be_rebuilt__should_generate_versions(
    page_model: type[BaseModel],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/nodes", response_model=page_model)
    async def get_nodes() -> dict[str, Any]:
        return {"items": [{"children": {"items": [{}]}}]}

    app = create_versioned_app(item_alias_version_change(), router=router)
    unversioned_client = TestClient(app)
    for version in ("2000-01-01", "2001-01-01"):
        response = TestClient(app, headers={app.router.api_version_parameter_name: version}).get("/nodes")
        assert response.status_code == HTTP_200_OK
        assert unversioned_client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK


@pytest.mark.parametrize(
    ("model_a", "model_b"),
    [(PartialA, PartialB), (DependencyA, DependencyB)],
    ids=["partial_default_factory", "dependency_of_default_factory"],
)
def test__router_generation__cycle_through_default_factory__should_not_depend_on_route_or_instruction_order(
    model_a: type[BaseModel],
    model_b: type[BaseModel],
    create_versioned_app: CreateVersionedApp,
):
    def get_behavior(routes_order: list[str], versioned_models: list[type[BaseModel]]) -> tuple[Any, ...]:
        router = VersionedAPIRouter()

        def get_a() -> dict[str, Any]:
            return {"fullName": "a"}

        def get_b() -> dict[str, Any]:
            return {"fullName": "b", "a": {"items": [{"fullName": "a"}]}}

        routes = {
            "get_a": lambda: router.add_api_route("/a", get_a, response_model=model_a),
            "get_b": lambda: router.add_api_route("/b", get_b, response_model=model_b),
        }
        for route in routes_order:
            routes[route]()
        app = create_versioned_app(
            version_change(*(old_name_alias(model) for model in versioned_models)), router=router
        )
        client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
        old_openapi = TestClient(app).get("/openapi.json?version=2000-01-01").json()
        return client_2000.get("/a").json(), client_2000.get("/b").json(), sorted(old_openapi["components"]["schemas"])

    behaviors = [
        get_behavior(routes_order, versioned_models)
        for routes_order in (["get_a", "get_b"], ["get_b", "get_a"])
        for versioned_models in ([model_a, model_b], [model_b, model_a])
    ]

    old_a, old_b, _ = behaviors[0]
    assert all(behavior == behaviors[0] for behavior in behaviors)
    # Both Page[A] and Page[B] are in the cycle so they keep their head args
    assert old_a == {"oldName": "a", "count": 0}
    assert old_b == {"oldName": "b", "a": {"items": [{"fullName": "a", "count": 0}]}}


def test__router_generation__unquoted_parametrization_of_cyclic_generic__should_fall_back_like_quoted_one(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/categories", response_model=Page[Category])
    async def get_categories() -> dict[str, Any]:
        return {"items": [{"fullName": "a", "subcategories": {"items": [{"fullName": "b"}]}}]}

    @router.post("/categories", response_model=Page[Category])
    async def create_categories(categories: Page[Category]) -> Page[Category]:
        return categories

    app = create_versioned_app(version_change(schema(Category).field("name").had(min_length=3)), router=router)
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert client_2000.get("/categories").status_code == HTTP_200_OK
    # Page[Category] keeps its head args like Page["Category"] within Category so the outer and the nested
    # categories are validated the same way
    body = {"items": [{"fullName": "abc", "subcategories": {"items": [{"fullName": "b"}]}}]}
    assert client_2000.post("/categories", json=body).status_code == HTTP_200_OK
    body = {"items": [{"fullName": "a", "subcategories": {"items": [{"fullName": "bcd"}]}}]}
    assert client_2000.post("/categories", json=body).status_code == HTTP_200_OK
    # Same schema names as without versioned type args
    assert sorted(unversioned_client.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]) == [
        "Category-Input",
        "Category-Output",
        "HTTPValidationError",
        "Page_Category_-Input",
        "Page_Category_-Output",
        "ValidationError",
    ]
