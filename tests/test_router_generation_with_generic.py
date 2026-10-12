import functools
import importlib.util
import sys
import threading
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Generic, Optional, TypeVar, Union

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, create_model
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_422_UNPROCESSABLE_CONTENT
from typing_extensions import Self, TypeAliasType, TypeVarTuple, Unpack, override

from cadwyn import (
    Cadwyn,
    HeadVersion,
    RequestInfo,
    Version,
    VersionBundle,
    convert_request_to_next_version_for,
    migrate_response_body,
)
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.schema_generation import generate_versioned_models
from cadwyn.structure.enums import enum
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


if TYPE_CHECKING:
    # They are only used in annotations that Python 3.14 evaluates lazily
    from asyncio import Lock as TypeCheckingOnlyLock  # noqa: TC004
    from builtins import list as type_checking_only_list  # noqa: TC004

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


class UnchangedItem(BaseModel):
    name: str = "a"


class UnchangedPair(BaseModel, Generic[ItemT, BoolT]):
    left: Optional[ItemT] = None
    right: Optional[BoolT] = None


class KindBase(BaseModel):
    def __init_subclass__(cls, *, kind: str, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.kind = kind


class KindItem(KindBase, kind="item"):
    name: str = "a"


class UnchangedArgsHolder(BaseModel):
    quoted: Optional[Page["UnchangedItem"]] = None
    quoted_in_list: Optional[Page[list["UnchangedItem"]]] = None
    quoted_in_dict: Optional[Page[dict[str, "UnchangedItem"]]] = None
    quoted_pair: Optional[UnchangedPair["UnchangedItem", int]] = None
    quoted_optional: Optional[Page["Optional[UnchangedItem]"]] = None
    optional_quoted: Optional[Page[Optional["UnchangedItem"]]] = None
    union_quoted: Optional[Page[Union["UnchangedItem", int]]] = None
    kind_items: Optional[Page[KindItem]] = None


class ItemWithUnusualAnnotations(BaseModel):
    item: Optional[Item] = None
    # typing.Callable cannot be rebuilt from its origin and args unlike collections.abc.Callable
    callback: typing.Callable = Field(default=lambda: None, exclude=True)
    values: tuple[int, ...] = Field(default_factory=make_values)
    metadata: dict[str, int] = Field(default_factory=functools.partial(dict, count=1))


class StripWithLock:
    """A validator that cannot be deep-copied"""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def __call__(self, value: str) -> str:
        with self.lock:
            return value.strip()


class LockedItem(BaseModel):
    name: Annotated[str, BeforeValidator(StripWithLock())]


class TaskStatus(str, Enum):
    open = "open"
    archived = "archived"


class Task(BaseModel):
    status: TaskStatus


class Unsupported:
    pass


class DeferredItem(BaseModel):
    """Pydantic cannot build its schema but it never has to because Ref does not use its type arg"""

    model_config = ConfigDict(defer_build=True)
    value: Unsupported


class ItemWithForwardRef(BaseModel):
    # Its fields are only resolved when it is rebuilt because DefinedLater does not exist yet
    other: "Optional[DefinedLater]" = None


class DefinedLater(BaseModel):
    name: str = Field(alias="fullName")


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


def old_name_description(model: type[BaseModel]):
    return schema(model).field("name").had(description="Old name")


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


def test__router_generation__generic_args_without_versioned_models__should_be_left_as_they_are(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()
    body = {"quoted": {"items": [{"name": "a"}]}, "kind_items": {"items": [{"name": "b"}]}}

    @router.post("/holders", response_model=UnchangedArgsHolder)
    async def create_holder(holder: UnchangedArgsHolder) -> UnchangedArgsHolder:
        return holder

    fastapi_app = FastAPI()
    fastapi_app.post("/holders", response_model=UnchangedArgsHolder)(create_holder)
    expected_schema_names = sorted(TestClient(fastapi_app).get("/openapi.json").json()["components"]["schemas"])
    app = create_versioned_app(item_alias_version_change(), router=router)
    unversioned_client = TestClient(app)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/holders", json=body).json()["kind_items"] == {"items": [{"name": "b"}]}
        # The same names as without Cadwyn because none of the args refer to versioned models
        assert sorted(unversioned_client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]) == (
            expected_schema_names
        )

    versions = VersionBundle(Version("2001-01-01", item_alias_version_change()), Version("2000-01-01"))
    assert migrate_response_body(
        versions, Page[UnchangedItem], latest_body={"items": [UnchangedItem()]}, version="2000-01-01"
    ).model_dump() == {"items": [{"name": "a"}]}


def test__migrate_response_body__concurrently_for_generics__should_generate_models_without_errors():
    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(5):
            leaves: list[Any] = [create_model(f"Leaf{i}", name=(str, "a")) for i in range(40)]
            middles: list[Any] = [
                create_model(f"Middle{i}", leaf=(Optional[leaf], None), page=(Optional[Page[next_leaf]], None))
                for i, (leaf, next_leaf) in enumerate(zip(leaves, [*leaves[1:], leaves[0]], strict=True))
            ]
            versions = VersionBundle(
                HeadVersion(),
                Version("2001-01-01", version_change(*(old_name_description(leaf) for leaf in leaves))),
                Version("2000-01-01"),
            )
            # Like in an app that has already started and generates the rest of the models lazily
            generate_versioned_models(versions)
            barrier = threading.Barrier(len(middles))

            def migrate(middle: Any, versions: VersionBundle = versions, barrier=barrier) -> None:
                barrier.wait()
                migrate_response_body(
                    versions,
                    Page[middle],
                    latest_body={"items": [{"leaf": {}, "page": {"items": [{}]}}]},
                    version="2000-01-01",
                )

            with ThreadPoolExecutor(max_workers=len(middles)) as executor:
                futures = [executor.submit(migrate, middle) for middle in middles]
            for future in futures:
                future.result()
    finally:
        sys.setswitchinterval(switch_interval)


def test__router_generation__generic_arg_with_annotations_that_cannot_be_rebuilt__should_use_arg_from_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=Page[ItemWithUnusualAnnotations])
    async def get_items() -> dict[str, Any]:
        return {"items": [{"item": {"fullName": "Ada"}}]}

    app = create_versioned_app(item_alias_version_change(), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    expected_item = {"values": [], "metadata": {"count": 1}}
    assert client_2000.get("/items").json() == {"items": [{"item": {"oldName": "Ada"}, **expected_item}]}
    assert client_2001.get("/items").json() == {"items": [{"item": {"fullName": "Ada"}, **expected_item}]}
    assert TestClient(app).get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK


@pytest.mark.parametrize(
    "version_changes",
    [[], [version_change(old_name_description(Item))]],
    ids=["no_instructions", "unrelated_instructions"],
)
def test__migrate_response_body__generic_args_without_versioned_models__should_not_be_copied(
    version_changes: list[type[VersionChange]],
):
    versions = VersionBundle(Version("2001-01-01", *version_changes), Version("2000-01-01"))

    migrated_body = migrate_response_body(
        versions, Page[LockedItem], latest_body={"items": [{"name": " Ada "}]}, version="2000-01-01"
    )

    assert migrated_body.model_dump() == {"items": [{"name": "Ada"}]}


def test__router_generation__generic_arg_referring_to_versioned_enum__should_use_enum_from_requested_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/tasks", response_model=Page[Task])
    async def create_tasks(tasks: Page[Task]) -> Page[Task]:
        return tasks

    app = create_versioned_app(version_change(enum(TaskStatus).didnt_have("archived")), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    body = {"items": [{"status": "archived"}]}
    assert client_2000.post("/tasks", json=body).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert client_2001.post("/tasks", json=body).json() == body


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Annotations are only evaluated lazily since Python 3.14")
@pytest.mark.parametrize(
    "version_changes",
    [[], [version_change(old_name_description(Item))]],
    ids=["no_instructions", "unrelated_instructions"],
)
def test__router_generation__generic_args_with_annotations_only_defined_for_type_checking__should_work(
    version_changes: list[type[VersionChange]],
):
    # These annotations would fail before Python 3.14 because they are evaluated when the class is created
    class ItemWithLock(BaseModel):
        name: str = "a"
        _lock: ClassVar[TypeCheckingOnlyLock | None] = None

    def get_default_values() -> type_checking_only_list[int]:
        return []

    class ItemWithValues(BaseModel):
        values: list[int] = Field(default_factory=get_default_values)

    router = VersionedAPIRouter()

    @router.get("/items_with_lock", response_model=Page[ItemWithLock])
    async def get_items_with_lock() -> dict[str, Any]:
        return {"items": [{"name": "a"}]}

    @router.get("/items_with_values", response_model=Page[ItemWithValues])
    async def get_items_with_values() -> dict[str, Any]:
        return {"items": [{}]}

    versions = VersionBundle(HeadVersion(), Version("2001-01-01", *version_changes), Version("2000-01-01"))
    app = Cadwyn(versions=versions)
    app.generate_and_include_versioned_routers(router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/items_with_lock").json() == {"items": [{"name": "a"}]}
        assert client.get("/items_with_values").json() == {"items": [{"values": []}]}
        assert client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK

    migrated_body = migrate_response_body(
        versions, Page[ItemWithLock], latest_body={"items": [{"name": "a"}]}, version="2000-01-01"
    )
    assert migrated_body.model_dump() == {"items": [{"name": "a"}]}
    migrated_body = migrate_response_body(
        versions, Page[ItemWithValues], latest_body={"items": [{}]}, version="2000-01-01"
    )
    assert migrated_body.model_dump() == {"items": [{"values": []}]}


@pytest.mark.parametrize(
    "version_changes",
    [[], [version_change(old_name_description(Item))]],
    ids=["no_instructions", "unrelated_instructions"],
)
def test__router_generation__generic_arg_whose_schema_cannot_be_built__should_not_be_built(
    version_changes: list[type[VersionChange]],
):
    router = VersionedAPIRouter()

    @router.get("/refs", response_model=Ref[DeferredItem])
    async def get_ref() -> dict[str, Any]:
        return {"id": 1}

    versions = VersionBundle(HeadVersion(), Version("2001-01-01", *version_changes), Version("2000-01-01"))
    app = Cadwyn(versions=versions)
    app.generate_and_include_versioned_routers(router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/refs").json() == {"id": 1}
        assert client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK

    migrated_body = migrate_response_body(versions, Ref[DeferredItem], latest_body={"id": 1}, version="2000-01-01")
    assert migrated_body.model_dump() == {"id": 1}


def test__router_generation__generic_arg_with_unresolved_forward_ref__should_use_arg_from_requested_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=Page[ItemWithForwardRef])
    async def get_items() -> dict[str, Any]:
        return {"items": [{"other": {"fullName": "Ada"}}]}

    app = create_versioned_app(version_change(old_name_alias(DefinedLater)), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/items").json() == {"items": [{"other": {"oldName": "Ada"}}]}
    assert client_2001.get("/items").json() == {"items": [{"other": {"fullName": "Ada"}}]}


def test__router_generation__untouched_generic_arg_referring_to_deferred_model__should_not_build_it(
    create_versioned_app: CreateVersionedApp,
):
    class DeferredKind(BaseModel):
        model_config = ConfigDict(defer_build=True)
        x: int = 1

        @classmethod
        @override
        def __pydantic_on_complete__(cls) -> None:
            raise AssertionError(f"{cls.__name__} must stay deferred")

    class ItemWithKinds(BaseModel):
        kinds: ClassVar[dict[str, type[DeferredKind]]] = {}
        name: str = "a"

    router = VersionedAPIRouter()

    @router.get("/items", response_model=Page[ItemWithKinds])
    async def get_items() -> dict[str, Any]:
        return {"items": [{}]}

    app = create_versioned_app(item_alias_version_change(), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert client_2000.get("/items").json() == {"items": [{"name": "a"}]}
    assert not DeferredKind.__pydantic_complete__


def test__generate_versioned_models__untouched_generic_arg_with_unresolvable_forward_ref__should_stay_unresolved():
    class ItemWithUnresolvableRef(BaseModel):
        name: str = "a"
        # Cadwyn's own local variables must not be used to resolve forward refs
        unresolvable: "Optional[model]" = None  # noqa: F821  # ty: ignore[unresolved-reference]

    versions = VersionBundle(Version("2001-01-01", item_alias_version_change()), Version("2000-01-01"))

    generate_versioned_models(versions)["2000-01-01"][Page[ItemWithUnresolvableRef]]

    assert not ItemWithUnresolvableRef.__pydantic_complete__


def load_fresh_forward_ref_cycles_module() -> Any:
    module_name = f"tests._data.generic_forward_ref_cycles_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).parent / "_data" / "generic_forward_ref_cycles.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("model_name", "body", "old_body"),
    [
        pytest.param(
            "NestedUnionA",
            {"changed": {"fullName": "x"}, "peer": {"id": 1}},
            {"changed": {"oldName": "x"}, "peer": {"id": 1}},
            id="forward_ref_to_class_namespace",
        ),
        pytest.param(
            "MetadataA",
            {"changed": {"fullName": "x"}},
            {"changed": {"oldName": "x"}, "detail": {"label": "1"}},
            id="cycle_through_unresolved_metadata",
        ),
    ],
)
@pytest.mark.parametrize("migrate_arg_first", [False, True], ids=["generic_first", "arg_first"])
def test__migrate_response_body__generic_arg_with_forward_refs_resolved_by_pydantic__should_find_their_cycles(
    model_name: str,
    body: dict[str, Any],
    old_body: dict[str, Any],
    migrate_arg_first: bool,
):
    module = load_fresh_forward_ref_cycles_module()
    model = getattr(module, model_name)
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                schema(module.Changed)
                .field("name")
                .had(
                    alias="oldName", validation_alias=AliasChoices("oldName", "fullName"), serialization_alias="oldName"
                )
            ),
        ),
        Version("2000-01-01"),
    )
    if migrate_arg_first:
        assert (
            migrate_response_body(versions, model, latest_body=body, version="2000-01-01").model_dump(by_alias=True)
            == old_body
        )

    migrated_body = migrate_response_body(
        versions, module.Page[model], latest_body={"items": [body]}, version="2000-01-01"
    )

    assert migrated_body.model_dump(by_alias=True) == {"items": [old_body]}
