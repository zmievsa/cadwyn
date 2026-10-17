import functools
import importlib.util
import sys
import threading
import typing
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    ForwardRef,
    Generic,
    Literal,
    Optional,
    TypeVar,
    Union,
)

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    ValidationError,
    create_model,
)
from pydantic.json_schema import SkipJsonSchema
from starlette.status import HTTP_200_OK, HTTP_204_NO_CONTENT, HTTP_422_UNPROCESSABLE_CONTENT
from typing_extensions import Self, TypeAliasType, TypeVarTuple, Unpack, override

from cadwyn import (
    Cadwyn,
    HeadVersion,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    migrate_response_body,
)
from cadwyn.exceptions import CadwynHeadRequestValidationError
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.schema_generation import (
    _contains_forward_refs,
    _eval_forward_refs,
    _evaluate_user_annotations,
    _get_head_model_references,
    _get_rebuilt_model_fields,
    generate_versioned_models,
)
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


# A version change can only be bound to one version bundle, so each app gets its own
def generic_version_change() -> type[VersionChange]:
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

    return GenericVersionChange


def parametrized_version_change() -> type[VersionChange]:
    class ParametrizedVersionChange(VersionChange):
        description = "Parametrized"
        instructions_to_migrate_to_previous_version = (
            schema(ParametrizedSchema).field("bar").had(description="A bar"),
            schema(ParametrizedSchema).field("bar").didnt_have("title"),
        )

    return ParametrizedVersionChange


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
        generic_version_change(),
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
        parametrized_version_change(),
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


# Only the annotations of its parameters are versioned, like FastAPI only uses them
def get_dependency_page(page: Optional[Page["DependencyB"]] = None) -> Any:
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


class DocumentedItem(BaseModel):
    """INTERNAL: head-only implementation note."""

    name: str


def test__router_generation__schema_had_description_of_generic_arg__should_change_description_in_older_openapi(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=Page[DocumentedItem])
    async def list_items() -> dict[str, Any]:
        raise NotImplementedError

    app = create_versioned_app(
        version_change(schema(DocumentedItem).had(description="An item in the catalog.")), router=router
    )
    client = TestClient(app)

    old_schemas = client.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    new_schemas = client.get("/openapi.json?version=2001-01-01").json()["components"]["schemas"]
    assert old_schemas["DocumentedItem"]["description"] == "An item in the catalog."
    assert new_schemas["DocumentedItem"]["description"] == "INTERNAL: head-only implementation note."
    assert old_schemas["Page_DocumentedItem_"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/DocumentedItem"
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

    versions = VersionBundle(
        Version("2001-01-01", item_alias_version_change()),
        Version("2000-01-01"),
    )
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


@pytest.mark.parametrize("has_instructions", [False, True], ids=["no_instructions", "unrelated_instructions"])
def test__migrate_response_body__generic_args_without_versioned_models__should_not_be_copied(has_instructions: bool):
    version_changes = [version_change(old_name_description(Item))] if has_instructions else []
    versions = VersionBundle(
        Version("2001-01-01", *version_changes),
        Version("2000-01-01"),
    )

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
@pytest.mark.parametrize("has_instructions", [False, True], ids=["no_instructions", "unrelated_instructions"])
def test__router_generation__generic_args_with_annotations_only_defined_for_type_checking__should_work(
    has_instructions: bool,
):
    version_changes = [version_change(old_name_description(Item))] if has_instructions else []

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

    versions = VersionBundle(
        HeadVersion(),
        Version("2001-01-01", *version_changes),
        Version("2000-01-01"),
    )
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


@pytest.mark.parametrize("has_instructions", [False, True], ids=["no_instructions", "unrelated_instructions"])
def test__router_generation__generic_arg_whose_schema_cannot_be_built__should_not_be_built(has_instructions: bool):
    version_changes = [version_change(old_name_description(Item))] if has_instructions else []
    router = VersionedAPIRouter()

    @router.get("/refs", response_model=Ref[DeferredItem])
    async def get_ref() -> dict[str, Any]:
        return {"id": 1}

    versions = VersionBundle(
        HeadVersion(),
        Version("2001-01-01", *version_changes),
        Version("2000-01-01"),
    )
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

    versions = VersionBundle(
        Version("2001-01-01", item_alias_version_change()),
        Version("2000-01-01"),
    )

    generate_versioned_models(versions)["2000-01-01"][Page[ItemWithUnresolvableRef]]

    assert not ItemWithUnresolvableRef.__pydantic_complete__


def load_fresh_data_module(name: str, **module_attributes: Any) -> Any:
    module_name = f"tests._data.{name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).parent / "_data" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(module_attributes)
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
    module = load_fresh_data_module("generic_forward_ref_cycles")
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


REFERENCE_KINDS_TO_LATER_NAMES = [
    "field",
    "generic_arg",
    "default_factory",
    *(
        pytest.param(
            kind, marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="Annotations are lazy since Python 3.14")
        )
        for kind in ["classvar", "lazy_default_factory"]
    ),
    *(
        pytest.param(
            kind, marks=pytest.mark.skipif(sys.version_info < (3, 12), reason="Type aliases are lazy since Python 3.12")
        )
        for kind in ["type_alias", "classvar_type_alias"]
    ),
]


@pytest.mark.parametrize("kind", REFERENCE_KINDS_TO_LATER_NAMES)
@pytest.mark.parametrize("refer_back_to_page", [True, False], ids=["cycle", "no_cycle"])
@pytest.mark.parametrize("look_through_other_generic", [True, False], ids=["through_box", "directly"])
def test__generate_versioned_models__generic_arg_looked_at_before_its_names_are_defined__should_keep_head_args(
    kind: str, refer_back_to_page: bool, look_through_other_generic: bool
):
    module = load_fresh_data_module("generic_stale_references", KIND=kind)
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(module.Other).field("value").didnt_exist)),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]
    # Looks at X while Later is not defined yet
    generator[module.Box[module.W] if look_through_other_generic else module.Page[module.X]]
    module.define_later(refer_back_to_page=refer_back_to_page)

    page_copy = generator[module.Page[module.X]]

    # Whatever Later turned out to be, the generator keeps the decision that it made without it, as on main
    page = page_copy.model_validate({"items": [{"other": {"value": 3}}]})
    assert page.model_dump()["items"][0]["other"] == {"value": 3}


@pytest.mark.parametrize("kind", REFERENCE_KINDS_TO_LATER_NAMES)
@pytest.mark.parametrize(
    ("refer_back_to_page", "old_other"),
    [pytest.param(True, {"value": 3}, id="cycle"), pytest.param(False, {}, id="no_cycle")],
)
def test__generate_versioned_models__generic_arg_whose_names_are_defined_before_looking_at_it__should_be_versioned(
    kind: str, refer_back_to_page: bool, old_other: dict[str, Any]
):
    module = load_fresh_data_module("generic_stale_references", KIND=kind)
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(module.Other).field("value").didnt_exist)),
        Version("2000-01-01"),
    )
    module.define_later(refer_back_to_page=refer_back_to_page)

    page_copy = generate_versioned_models(versions)["2000-01-01"][module.Page[module.X]]

    page = page_copy.model_validate({"items": [{"other": {"value": 3}}]})
    assert page.model_dump()["items"][0]["other"] == old_other


def test__generate_versioned_models__generic_whose_own_args_have_names_defined_later__should_keep_head_args():
    module = load_fresh_data_module("generic_stale_references", KIND="field")
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(module.Other).field("value").didnt_exist)),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]
    # Looks at Ref[Union[Other, "Later"]] while Later is not defined yet
    generator[module.Box[module.W]]
    module.define_later(refer_back_to_page=False, refer_back_to_ref_of_later=True)

    ref_copy = generator[module.RefOfLater]

    assert ref_copy.__pydantic_generic_metadata__["args"] == module.RefOfLater.__pydantic_generic_metadata__["args"]


RECURSIVE_TYPE_ALIAS_KINDS = [
    pytest.param(
        "native",
        marks=pytest.mark.skipif(sys.version_info < (3, 12), reason="Native type aliases exist since Python 3.12"),
    ),
    "typing_extensions",
]


@pytest.mark.parametrize("alias_kind", RECURSIVE_TYPE_ALIAS_KINDS)
@pytest.mark.parametrize("changed_model_name", [None, "Other", "Leaf"], ids=["no_changes", "unrelated", "reachable"])
@pytest.mark.parametrize("generic_arg_name", ["Item", "JsonData"])
def test__router_generation__generic_arg_with_recursive_type_alias__should_work_as_on_main(
    create_versioned_app: CreateVersionedApp,
    alias_kind: str,
    changed_model_name: Union[str, None],
    generic_arg_name: str,
):
    module = load_fresh_data_module("generic_recursive_type_aliases", KIND=alias_kind)
    page = module.Page[getattr(module, generic_arg_name)]
    item = {"data": {"nested": [1, {"value": 5}]}}
    body = {"items": [item if generic_arg_name == "Item" else item["data"]]}
    version_changes = (
        [version_change(schema(getattr(module, changed_model_name)).field("value").didnt_exist)]
        if changed_model_name
        else []
    )
    router = VersionedAPIRouter()

    @router.get("/items", response_model=page)
    async def get_items() -> dict[str, Any]:
        return body

    app = create_versioned_app(*version_changes, router=router)
    versions = app.versions
    clients = {
        version: TestClient(app, headers={app.router.api_version_parameter_name: version})
        for version in app.router.versioned_routers
    }

    for version, client in clients.items():
        assert client.get("/items").json() == body
        assert client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK
        assert migrate_response_body(versions, page, latest_body=body, version=version).model_dump() == body


EventTypeT = TypeVar("EventTypeT")


class Event(BaseModel, Generic[EventTypeT, ItemT]):
    type: EventTypeT
    data: ItemT


@pytest.mark.parametrize(
    ("annotation", "contains_forward_refs"),
    [
        (int, False),
        ("Item", True),
        (ForwardRef("Item"), True),
        (list["Item"], True),
        (dict[str, "Item"], True),
        (Union[int, "Item"], True),
        (Optional[list["Item"]], True),
        (Annotated["Item", "doc"], True),
        (Literal["user.created"], False),
        (Optional[Literal["user.created"]], False),
        (dict[str, Literal["x", "y"]], False),
        (Annotated[int, "doc"], False),
        (Annotated[list[int], "doc", Field(description="doc")], False),
        (Event[Literal["user.created"], Item], False),
    ],
)
def test__contains_forward_refs__should_only_find_strings_that_typing_evaluates(
    annotation: Any, contains_forward_refs: bool
):
    assert _contains_forward_refs(annotation) is contains_forward_refs


class EventHolder(BaseModel):
    event: Event[Literal["item.created"], Item]


class AnnotatedItemsHolder(BaseModel):
    items: Page[Annotated[Item, "An item"]]


@pytest.mark.parametrize(
    ("response_model", "body", "old_body"),
    [
        pytest.param(
            Event[Literal["item.created"], Item],
            {"type": "item.created", "data": {"fullName": "a"}},
            {"type": "item.created", "data": {"oldName": "a"}},
            id="literal_arg",
        ),
        pytest.param(
            Page[Annotated[Item, "An item"]],
            {"items": [{"fullName": "a"}]},
            {"items": [{"oldName": "a"}]},
            id="annotated_arg_with_string_metadata",
        ),
        pytest.param(
            Page[Event[Literal["item.created"], Item]],
            {"items": [{"type": "item.created", "data": {"fullName": "a"}}]},
            {"items": [{"type": "item.created", "data": {"oldName": "a"}}]},
            id="page_of_literal_arg",
        ),
        pytest.param(
            Page[EventHolder],
            {"items": [{"event": {"type": "item.created", "data": {"fullName": "a"}}}]},
            {"items": [{"event": {"type": "item.created", "data": {"oldName": "a"}}}]},
            id="page_of_model_with_literal_arg",
        ),
        pytest.param(
            Page[AnnotatedItemsHolder],
            {"items": [{"items": {"items": [{"fullName": "a"}]}}]},
            {"items": [{"items": {"items": [{"oldName": "a"}]}}]},
            id="page_of_model_with_annotated_arg",
        ),
    ],
)
def test__router_generation__generic_args_with_strings_that_are_not_types__should_be_versioned(
    create_versioned_app: CreateVersionedApp,
    response_model: Any,
    body: dict[str, Any],
    old_body: dict[str, Any],
):
    router = VersionedAPIRouter()

    @router.get("/events", response_model=response_model)
    async def get_events() -> dict[str, Any]:
        return body

    @router.get("/items", response_model=Page[Item])
    async def get_items() -> dict[str, Any]:
        return {"items": [{"fullName": "b"}]}

    app = create_versioned_app(item_alias_version_change(), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert client_2000.get("/events").json() == old_body
    assert client_2000.get("/items").json() == {"items": [{"oldName": "b"}]}
    # Both routes use the same old Item instead of the head one and the old one side by side
    schema_names = client_2000.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert "Item" in schema_names
    assert not [name for name in schema_names if name.endswith(("__1", "__2"))]


class QuotedItemsHolder(BaseModel):
    quoted: Optional[Page["Item"]] = None
    quoted_in_list: Optional[Page[list["Item"]]] = None
    quoted_in_dict: Optional[Page[dict[str, "Item"]]] = None
    quoted_in_optional_list: Optional[Page[Optional[list["Item"]]]] = None
    quoted_in_annotated_list: Optional[Page[Annotated[list["Item"], "Items"]]] = None
    quoted_next_to_literal: Optional[Page[Union[Literal["none"], list["Item"]]]] = None
    quoted_in_union_type: Optional[Page[list["Item"] | None]] = None


def test__router_generation__strings_nested_in_builtin_generic_args__should_be_versioned(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=QuotedItemsHolder)
    async def get_items() -> dict[str, Any]:
        return {
            "quoted": {"items": [{"fullName": "a"}]},
            "quoted_in_list": {"items": [[{"fullName": "b"}]]},
            "quoted_in_dict": {"items": [{"key": {"fullName": "c"}}]},
            "quoted_in_optional_list": {"items": [[{"fullName": "d"}]]},
            "quoted_in_annotated_list": {"items": [[{"fullName": "e"}]]},
            "quoted_next_to_literal": {"items": ["none", [{"fullName": "f"}]]},
            "quoted_in_union_type": {"items": [[{"fullName": "g"}]]},
        }

    app = create_versioned_app(item_alias_version_change(), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert client_2000.get("/items").json() == {
        "quoted": {"items": [{"oldName": "a"}]},
        "quoted_in_list": {"items": [[{"oldName": "b"}]]},
        "quoted_in_dict": {"items": [{"key": {"oldName": "c"}}]},
        "quoted_in_optional_list": {"items": [[{"oldName": "d"}]]},
        "quoted_in_annotated_list": {"items": [[{"oldName": "e"}]]},
        "quoted_next_to_literal": {"items": ["none", [{"oldName": "f"}]]},
        "quoted_in_union_type": {"items": [[{"oldName": "g"}]]},
    }
    schema_names = client_2000.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert {"Page_Item_", "Page_list_Item__", "Page_dict_str__Item__", "Item"} <= set(schema_names)
    assert not [name for name in schema_names if name.endswith(("__1", "__2"))]


@pytest.mark.parametrize("version", ["2000-01-01", "2001-01-01"])
def test__migrate_response_body__head_instances_in_versioned_generic_args__should_be_rejected(version: str):
    versions = VersionBundle(
        Version("2001-01-01", version_change(old_name_description(Item))),
        Version("2000-01-01"),
    )

    # Like in versioned models that are not generic, the versioned args only accept data
    with pytest.raises(ValidationError):
        migrate_response_body(versions, Page[Item], latest_body={"items": [Item(fullName="a")]}, version=version)
    migrated_body = migrate_response_body(
        versions, Page[Item], latest_body={"items": [Item(fullName="a").model_dump(by_alias=True)]}, version=version
    )
    assert migrated_body.model_dump() == {"items": [{"name": "a"}]}


def test__get_head_model_references__model_with_unresolved_annotated_field__should_not_change_its_fields():
    module = load_fresh_data_module("generic_incomplete_annotated_fields")
    assert not module.Parent.__pydantic_fields_complete__
    assignment = module.Parent.__pydantic_fields__["child"]._original_assignment
    metadata = list(assignment.metadata)

    # Once per version, which pydantic 2.11 would turn into one more validator each time
    for _ in range(3):
        assert _get_head_model_references(module.Parent).is_complete

    assert assignment.metadata == metadata
    assert module.Parent().child is None


class OneItemBox(BaseModel, Generic[ItemT]):
    item: ItemT


class ItemState(str, Enum):
    active = "active"


class StrictItemWithState(BaseModel):
    model_config = ConfigDict(strict=True)
    state: ItemState


@pytest.mark.parametrize(
    ("response_model", "body"),
    [
        pytest.param(
            OneItemBox[StrictItemWithState],
            OneItemBox[StrictItemWithState](item=StrictItemWithState(state=ItemState.active)),
            id="generic_arg",
        ),
        # This failed on main too
        pytest.param(StrictItemWithState, StrictItemWithState(state=ItemState.active), id="model"),
    ],
)
def test__router_generation__strict_model_with_unchanged_enum__should_accept_head_enum_members(
    create_versioned_app: CreateVersionedApp,
    response_model: Any,
    body: BaseModel,
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=response_model)
    async def get_items() -> Any:
        return body

    app = create_versioned_app(
        version_change(schema(StrictItemWithState).field("state").had(description="Old")), router=router
    )

    for version in ["2000-01-01", "2001-01-01"]:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/items").json() == body.model_dump(mode="json")
        assert "ItemState" in client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]


def test__generate_versioned_models__enums_without_instructions__should_not_be_copied():
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(StrictItemWithState).field("state").had(description="Old"))),
        Version("2000-01-01"),
    )

    for generator in generate_versioned_models(versions).values():
        assert generator[ItemState] is ItemState
        assert generator[StrictItemWithState].model_fields["state"].annotation is ItemState


@pytest.mark.parametrize("version", ["2000-01-01", "2001-01-01"])
def test__migrate_response_body__head_enum_members_in_strict_generic_args__should_be_accepted(version: str):
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(StrictItemWithState).field("state").had(description="Old"))),
        Version("2000-01-01"),
    )

    migrated_body = migrate_response_body(
        versions,
        OneItemBox[StrictItemWithState],
        latest_body={"item": {"state": ItemState.active}},
        version=version,
    )

    assert migrated_body.model_dump() == {"item": {"state": ItemState.active}}


@pytest.mark.parametrize(
    "kind",
    [
        "default_factory_type_error",
        "default_factory_attribute_error",
        "dependency_of_default_factory",
        "quoted_generic_arg",
        pytest.param(
            "type_alias",
            marks=pytest.mark.skipif(sys.version_info < (3, 12), reason="Native type aliases exist since Python 3.12"),
        ),
        pytest.param(
            "lazy_default_factory",
            marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="Annotations are lazy since Python 3.14"),
        ),
    ],
)
@pytest.mark.parametrize("has_instructions", [True, False], ids=["unrelated_instructions", "no_instructions"])
def test__router_generation__generic_arg_with_annotations_failing_at_runtime__should_work_as_on_main(
    create_versioned_app: CreateVersionedApp,
    kind: str,
    has_instructions: bool,
):
    if kind == "type_alias":
        module = load_fresh_data_module("generic_arg_with_stub_only_type_alias")
    elif kind == "lazy_default_factory":
        module = load_fresh_data_module("generic_arg_with_lazy_stub_only_annotations")
    else:
        module = load_fresh_data_module("generic_args_with_annotations_failing_at_runtime", KIND=kind)
    router = VersionedAPIRouter()

    @router.get("/items", response_model=module.Page[module.Item])
    async def get_items() -> dict[str, Any]:
        return {"items": [{"name": "b"}]}

    version_changes = [version_change(schema(module.Other).field("value").had(description="Old"))]
    app = create_versioned_app(*(version_changes if has_instructions else []), router=router)

    for version in app.router.versioned_routers:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/items").json()["items"][0]["name"] == "b"
        assert client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK


def test__generate_versioned_models__without_changed_models__should_keep_head_generic_args():
    versions = VersionBundle(HeadVersion(), Version("2001-01-01"), Version("2000-01-01"))

    for generator in generate_versioned_models(versions).values():
        assert generator[Page[Item]].__pydantic_generic_metadata__["args"] == (Item,)
        assert generator[Page[Item]].model_fields["items"].annotation == list[Item]


class NodeWithExplicitForwardRef(BaseModel):
    name: str = "n"
    parent: ForwardRef("Optional[NodeWithExplicitForwardRef]") = None  # ty: ignore[invalid-type-form]


class HolderWithExplicitForwardRef(BaseModel):
    item: ForwardRef("Item")  # ty: ignore[invalid-type-form]


NodeWithExplicitForwardRef.model_rebuild()
HolderWithExplicitForwardRef.model_rebuild()


@pytest.mark.parametrize(
    ("response_model", "body"),
    [
        pytest.param(NodeWithExplicitForwardRef, {"name": "a", "parent": {"name": "b", "parent": None}}, id="self"),
        # Main keeps explicit forward refs as they are so the head Item is used, unlike for quoted annotations
        pytest.param(HolderWithExplicitForwardRef, {"item": {"fullName": "a"}}, id="changed_model"),
    ],
)
def test__router_generation__explicit_forward_refs__should_work_as_on_main(
    create_versioned_app: CreateVersionedApp,
    response_model: type[BaseModel],
    body: dict[str, Any],
):
    router = VersionedAPIRouter()

    @router.get("/items", response_model=response_model)
    async def get_items() -> dict[str, Any]:
        return body

    app = create_versioned_app(item_alias_version_change(), router=router)

    for version in app.router.versioned_routers:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/items").json() == body
        assert client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK


def make_tags_from_registry(registry: Optional[dict[str, Any]] = None) -> list[str]:
    return []


TAG_FACTORIES: dict[str, Any] = {"tags": make_tags_from_registry}
make_tags_from_registry.__defaults__ = (TAG_FACTORIES,)


def make_tags_with_callback(callback: Any = None) -> list[str]:
    return []


def make_more_tags_with_callback(callback: Any = make_tags_with_callback) -> list[str]:  # pragma: no cover
    return []  # Only its default is used


make_tags_with_callback.__defaults__ = (make_more_tags_with_callback,)
SELF_CONTAINING_LIST: list[Any] = []
SELF_CONTAINING_LIST.append(SELF_CONTAINING_LIST)


def make_tags_with_self_containing_default(values: Any = SELF_CONTAINING_LIST) -> list[str]:
    return []


@pytest.mark.parametrize(
    "default_factory",
    [make_tags_from_registry, make_tags_with_callback, make_tags_with_self_containing_default],
    ids=["registry_with_the_factory", "factories_referring_to_each_other", "self_containing_default"],
)
def test__router_generation__generic_arg_with_default_factory_whose_defaults_refer_back__should_work_as_on_main(
    create_versioned_app: CreateVersionedApp,
    default_factory: Any,
):
    class ItemWithCyclicFactory(BaseModel):
        name: str = "a"
        tags: list[str] = Field(default_factory=default_factory)

    class Unrelated(BaseModel):
        value: int = 1

    router = VersionedAPIRouter()

    @router.get("/items", response_model=Page[ItemWithCyclicFactory])
    async def get_items() -> dict[str, Any]:
        return {"items": [{"name": "b"}]}

    app = create_versioned_app(version_change(schema(Unrelated).field("value").had(description="Old")), router=router)

    for version in app.router.versioned_routers:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/items").json() == {"items": [{"name": "b", "tags": []}]}


def test__evaluate_user_annotations__recursion_error__should_not_be_treated_as_unresolved():
    def recurse() -> Any:
        raise RecursionError

    with pytest.raises(RecursionError):
        _evaluate_user_annotations(recurse)


class ModelWithUnresolvedFieldForPlumbing(BaseModel):
    value: "Optional[DefinedForPlumbingLater]" = None  # noqa: F821  # ty: ignore[unresolved-reference]


def test__get_rebuilt_model_fields__failure_outside_of_user_annotations__should_not_be_treated_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("Cadwyn's own bug")

    assert not ModelWithUnresolvedFieldForPlumbing.__pydantic_fields_complete__
    monkeypatch.setattr("cadwyn.schema_generation.get_model_typevars_map", fail)

    with pytest.raises(TypeError, match="Cadwyn's own bug"):
        _get_rebuilt_model_fields(ModelWithUnresolvedFieldForPlumbing)


def test__eval_forward_refs__failure_outside_of_user_annotations__should_not_be_treated_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("Cadwyn's own bug")

    monkeypatch.setattr("cadwyn.schema_generation.unpack_lenient_weakvaluedict", fail)

    with pytest.raises(TypeError, match="Cadwyn's own bug"):
        _eval_forward_refs("Item", Page[Item])


class BodyWithState(BaseModel):
    state: ItemState


def test__router_generation__request_body_with_unchanged_enum__should_validate_in_every_version(
    create_versioned_app: CreateVersionedApp,
):
    class Unrelated(BaseModel):
        value: int = 1

    router = VersionedAPIRouter()

    @router.post("/items")
    async def post_item(body: BodyWithState) -> dict[str, Any]:
        return {"is_head_member": body.state is ItemState.active}

    app = create_versioned_app(version_change(schema(Unrelated).field("value").had(description="Old")), router=router)

    # This raised CadwynHeadRequestValidationError on main because the head body rejected the copy's enum member
    for version in app.router.versioned_routers:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/items", json={"state": "active"}).json() == {"is_head_member": True}


class RenamedItem(BaseModel):
    id: int
    name: str


def get_schema_names(app: Cadwyn, version: str) -> list[str]:
    openapi = TestClient(app).get(f"/openapi.json?version={version}").json()
    return sorted(openapi["components"]["schemas"])


def test__router_generation__generic_with_renamed_arg_field__should_version_arg(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/item", response_model=RenamedItem)
    async def get_item() -> RenamedItem:
        return RenamedItem(id=1, name="a")

    @router.post("/item", response_model=RenamedItem)
    async def post_item(item: RenamedItem) -> RenamedItem:
        return item

    @router.get("/page", response_model=Page[RenamedItem])
    async def get_page() -> Page[RenamedItem]:
        return Page[RenamedItem](items=[RenamedItem(id=1, name="a")])

    @router.post("/page", response_model=Page[RenamedItem])
    async def post_page(page: Page[RenamedItem]) -> Page[RenamedItem]:
        return page

    # The usual converters of the model, which do not run for the items of the generic
    @convert_response_to_previous_version_for(RenamedItem)
    def rename_name_to_title(response: ResponseInfo) -> None:
        response.body["title"] = response.body.pop("name")

    @convert_request_to_next_version_for(RenamedItem)
    def rename_title_to_name(request: RequestInfo) -> None:
        request.body["name"] = request.body.pop("title")

    app = create_versioned_app(
        version_change(
            schema(RenamedItem).field("name").had(name="title"),
            rename_name_to_title=rename_name_to_title,
            rename_title_to_name=rename_title_to_name,
        ),
        router=router,
    )
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    latest_body = {"items": [{"id": 1, "name": "a"}]}
    old_body = {"items": [{"id": 1, "title": "a"}]}

    assert old_client.get("/item").json() == {"id": 1, "title": "a"}
    assert old_client.post("/item", json={"id": 1, "title": "a"}).json() == {"id": 1, "title": "a"}
    head_shape_response = old_client.post("/page", json=latest_body)
    old_schema_names = [
        name for name in get_schema_names(app, "2000-01-01") if "RenamedItem" in name and not name.startswith("Page")
    ]
    assert head_shape_response.status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert head_shape_response.json()["detail"][0]["loc"] == ["body", "items", 0, "title"]
    assert old_schema_names == ["RenamedItem"]
    # The generic uses the old RenamedItem, so it needs converters of its own to migrate the items
    with pytest.raises(ResponseValidationError):
        old_client.get("/page")
    with pytest.raises(CadwynHeadRequestValidationError):
        old_client.post("/page", json=old_body)
    latest_client = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert latest_client.post("/page", json=latest_body).json() == latest_body
    assert get_schema_names(app, "2001-01-01") == [
        "HTTPValidationError",
        "Page_RenamedItem_",
        "RenamedItem",
        "ValidationError",
    ]


class ItemWithInternalField(BaseModel):
    id: int
    internal: str = "secret"


def test__router_generation__generic_arg_changed_in_head_version__should_change_in_generic(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/item", response_model=ItemWithInternalField)
    async def get_item() -> dict[str, Any]:
        return {"id": 1}

    @router.get("/page", response_model=Page[ItemWithInternalField])
    async def get_page() -> dict[str, Any]:
        return {"items": [{"id": 1}]}

    @router.get("/quoted", response_model=Page[list["ItemWithInternalField"]])
    async def get_quoted() -> dict[str, Any]:
        return {"items": [[{"id": 1}]]}

    app = create_versioned_app(
        head_version_changes=[version_change(schema(ItemWithInternalField).field("internal").didnt_exist)],
        router=router,
    )

    assert list(app.router.versioned_routers) == ["2000-01-01"]
    for version in app.router.versioned_routers:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/item").json() == {"id": 1}
        schema_names = get_schema_names(app, version)
        assert client.get("/page").json() == {"items": [{"id": 1}]}
        assert client.get("/quoted").json() == {"items": [[{"id": 1}]]}
        # The quoted arg is evaluated to version it
        assert "Page_list_ItemWithInternalField__" in schema_names
        assert [name for name in schema_names if "ItemWithInternalField" in name] == [
            "ItemWithInternalField",
            "Page_ItemWithInternalField_",
            "Page_list_ItemWithInternalField__",
        ]


class ItemColor(str, Enum):
    red = "red"
    blue = "blue"


class ItemWithColor(BaseModel):
    color: ItemColor
    category: "Optional[CategoryOfItemWithColor]" = None


class CategoryOfItemWithColor(BaseModel):
    name: str
    parent: "Optional[CategoryOfItemWithColor]" = None


class ItemWithStripWithLock(BaseModel):
    name: Annotated[str, BeforeValidator(StripWithLock())]
    item: Optional[RenamedItem] = None


def test__migrate_response_body__generic_args_referring_to_changed_models__should_reject_latest_models():
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(RenamedItem).field("name").had(description="Old"))),
        Version("2000-01-01"),
    )

    for version in ("2000-01-01", "2001-01-01"):
        latest_body = {"items": [RenamedItem(id=1, name="a")]}
        with pytest.raises(ValidationError):
            migrate_response_body(versions, Page[RenamedItem], latest_body=latest_body, version=version)
        # Copying the arg needs a deep copy of its validator, which cannot be copied
        with pytest.raises(TypeError, match="pickle"):
            migrate_response_body(
                versions, Page[ItemWithStripWithLock], latest_body={"items": [{"name": " a "}]}, version=version
            )


def test__router_generation__generic_arg_with_changed_enum_and_self_referencing_model__should_be_versioned(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/items", response_model=Page[ItemWithColor])
    async def post_items(page: Page[ItemWithColor]) -> Page[ItemWithColor]:
        return page

    app = create_versioned_app(version_change(enum(ItemColor).didnt_have("blue")), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    body = {"items": [{"color": "blue", "category": {"name": "c", "parent": None}}]}

    assert old_client.post("/items", json=body).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    red_body = {"items": [{"color": "red", "category": {"name": "c", "parent": None}}]}
    assert old_client.post("/items", json=red_body).json() == red_body


_InheritedT = TypeVar("_InheritedT")
_InheritedU = TypeVar("_InheritedU")


@pytest.mark.parametrize("shape", ["parametrization", "subclass_of_parametrization", "two_levels_of_generics"])
def test__router_generation__instruction_on_field_inherited_through_parametrized_generic__should_keep_its_type(
    create_versioned_app: CreateVersionedApp, shape: str
):
    class Pair(BaseModel, Generic[_InheritedT, _InheritedU]):
        first: _InheritedT
        second: _InheritedU

    if shape == "parametrization":
        model: Any = Pair[str, int]
    elif shape == "subclass_of_parametrization":

        class StrIntPair(Pair[str, int]):
            pass

        model = StrIntPair
    else:

        class IntPair(Pair[_InheritedU, int], Generic[_InheritedU]):
            pass

        model = IntPair[str]

    router = VersionedAPIRouter()

    @router.post("/pair", response_model=model)
    async def create_pair(body: model) -> Any:
        return body

    app = create_versioned_app(
        version_change(schema(model).field("second").had(description="Old second")), router=router
    )

    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/pair", json={"first": "a", "second": 1}).json() == {"first": "a", "second": 1}
        response = client.post("/pair", json={"first": "a", "second": "not a number"})
        assert response.status_code == HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["detail"][0]["loc"] == ["body", "second"]

    old_schemas = TestClient(app).get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert {name: schema["properties"]["second"] for name, schema in old_schemas.items() if "Pair" in name} == {
        name: {"type": "integer", "title": "Second", "description": "Old second"}
        for name in old_schemas
        if "Pair" in name
    }


class _InheritedItem(BaseModel):
    name: str


class _WithMeta(BaseModel, Generic[_InheritedT]):
    meta: _InheritedT


class _ItemsPage(BaseModel, Generic[_InheritedT]):
    items: list[_InheritedT]


class _PageMeta(BaseModel):
    total: int


# Sibling bases reuse the same TypeVar, as most codebases do
class _ItemsPageWithMeta(_WithMeta[_PageMeta], _ItemsPage[_InheritedT], Generic[_InheritedT]):
    pass


class _A(BaseModel, Generic[_InheritedT]):
    a: _InheritedT


class _B(BaseModel, Generic[_InheritedT]):
    b: _InheritedT


class _B2(BaseModel, Generic[_InheritedU]):
    b: _InheritedU


class _M(_A[int], _B[_InheritedT], Generic[_InheritedT]):
    pass


class _MS(_M[str]):
    pass


class _M2(_A[int], _B2[_InheritedT], Generic[_InheritedT]):
    pass


class _M3(_A[int], _B):
    pass


class _IntsRoot(RootModel[list[int]]):
    pass


class _GenericRoot(RootModel[list[_InheritedT]], Generic[_InheritedT]):
    pass


class _IntGenericRoot(_GenericRoot[int]):
    pass


@pytest.mark.parametrize(
    ("route_model", "changed_model", "field_name", "valid_body", "invalid_body"),
    [
        pytest.param(
            _ItemsPageWithMeta[_InheritedItem],
            _ItemsPageWithMeta,
            "items",
            {"meta": {"total": 1}, "items": [{"name": "a"}]},
            {"meta": {"total": 1}, "items": [{"total": 1}]},
            id="sibling_base_with_same_type_var",
        ),
        pytest.param(_M[str], _M[str], "b", {"a": 1, "b": "s"}, {"a": 1, "b": 5}, id="sibling_parametrization"),
        pytest.param(_MS, _MS, "b", {"a": 1, "b": "s"}, {"a": 1, "b": 5}, id="subclass_with_sibling_parametrization"),
        pytest.param(_M2[str], _M2[str], "b", {"a": 1, "b": "s"}, {"a": 1, "b": 5}, id="sibling_with_other_type_var"),
        # Pydantic leaves the type var of the bare generic base as it is, so the field accepts anything
        pytest.param(_M3, _M3, "b", {"a": 1, "b": "s"}, None, id="bare_generic_next_to_parametrization"),
        pytest.param(_IntsRoot, _IntsRoot, "root", [1], ["zz"], id="root_model_subclass"),
        pytest.param(_GenericRoot[int], _GenericRoot[int], "root", [1], ["zz"], id="generic_root_model"),
        pytest.param(_IntGenericRoot, _IntGenericRoot, "root", [1], ["zz"], id="subclass_of_generic_root_model"),
    ],
)
def test__router_generation__instruction_on_field_inherited_through_other_bases__should_keep_its_type(
    create_versioned_app: CreateVersionedApp,
    route_model: Any,
    changed_model: Any,
    field_name: str,
    valid_body: Any,
    invalid_body: Any,
):
    router = VersionedAPIRouter()

    @router.post("/models", response_model=route_model)
    async def create_model(body: route_model) -> Any:
        return body

    app = create_versioned_app(
        version_change(schema(changed_model).field(field_name).had(description="Old field")), router=router
    )

    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/models", json=valid_body).json() == valid_body
        if invalid_body is None:
            assert client.post("/models", json={**valid_body, field_name: 5}).status_code == HTTP_200_OK
        else:
            assert client.post("/models", json=invalid_body).status_code == HTTP_422_UNPROCESSABLE_CONTENT

    # The field has the same schema in the older version as in the newer one, except for its description
    old_openapi, new_openapi = (
        TestClient(app).get(f"/openapi.json?version={version}").json() for version in ("2000-01-01", "2001-01-01")
    )
    old_field_schema = _get_request_body_field_schema(old_openapi, field_name)
    assert old_field_schema.pop("description") == "Old field"
    assert old_field_schema == _get_request_body_field_schema(new_openapi, field_name)


def _get_request_body_field_schema(openapi: dict[str, Any], field_name: str) -> dict[str, Any]:
    ref = openapi["paths"]["/models"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    model_schema = openapi["components"]["schemas"][ref.removeprefix("#/components/schemas/")]
    return model_schema if field_name == "root" else model_schema["properties"][field_name]


_T = TypeVar("_T")
_U = TypeVar("_U")


def _create_app_with_instructions_for_parametrization_reached_through_generic(
    create_versioned_app: CreateVersionedApp, box_model: Any, pair_model: Any, box_body: dict[str, Any]
) -> dict[str, TestClient]:
    router = VersionedAPIRouter()

    @router.get("/pair", response_model=pair_model)
    async def get_pair() -> dict[str, Any]:
        return {"first": "a", "second": 1}

    @router.get("/box", response_model=box_model)
    async def get_box() -> dict[str, Any]:
        return box_body

    app = create_versioned_app(
        version_change(schema(pair_model).field("second").had(serialization_alias="old_second")), router=router
    )
    return {
        version: TestClient(app, headers={app.router.api_version_parameter_name: version})
        for version in ("2000-01-01", "2001-01-01")
    } | {"unversioned": TestClient(app)}


@pytest.mark.parametrize("field_shape", ["direct", "optional", "list", "nested_generic"])
def test__router_generation__parametrization_with_instructions_reached_through_generic__should_apply_them(
    create_versioned_app: CreateVersionedApp, field_shape: str
):
    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    if field_shape == "direct":

        class Box(BaseModel, Generic[_T]):
            pair: Pair[_T, int]

        new_body = {"pair": {"first": "a", "second": 1}}
        old_body = {"pair": {"first": "a", "old_second": 1}}
    elif field_shape == "optional":

        class Box(BaseModel, Generic[_T]):
            pair: Optional[Pair[_T, int]] = None

        new_body = {"pair": {"first": "a", "second": 1}}
        old_body = {"pair": {"first": "a", "old_second": 1}}
    elif field_shape == "list":

        class Box(BaseModel, Generic[_T]):
            pairs: list[Pair[_T, int]]

        new_body = {"pairs": [{"first": "a", "second": 1}]}
        old_body = {"pairs": [{"first": "a", "old_second": 1}]}
    else:

        class Inner(BaseModel, Generic[_T]):
            pair: Pair[_T, int]

        class Box(BaseModel, Generic[_T]):
            inner: Inner[_T]

        new_body = {"inner": {"pair": {"first": "a", "second": 1}}}
        old_body = {"inner": {"pair": {"first": "a", "old_second": 1}}}

    clients = _create_app_with_instructions_for_parametrization_reached_through_generic(
        create_versioned_app, Box[str], Pair[str, int], new_body
    )

    assert clients["2000-01-01"].get("/pair").json() == {"first": "a", "old_second": 1}
    assert clients["2000-01-01"].get("/box").json() == old_body
    assert clients["2001-01-01"].get("/pair").json() == {"first": "a", "second": 1}
    assert clients["2001-01-01"].get("/box").json() == new_body

    old_schemas = clients["unversioned"].get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert [name for name in old_schemas if "Pair" in name] == ["Pair_str_int_"]
    assert "old_second" in old_schemas["Pair_str_int_"]["properties"]


def test__router_generation__generic_built_before_parametrization_with_instructions__should_apply_them(
    create_versioned_app: CreateVersionedApp,
):
    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    class Box(BaseModel, Generic[_T]):
        pair: Pair[_T, int]

    # E.g. the routes are defined before the version changes. Pydantic 2.11 then builds a Pair[str, int] for Box[str]
    # that is a different class from the Pair[str, int] of the instructions.
    box_model = Box[str]
    clients = _create_app_with_instructions_for_parametrization_reached_through_generic(
        create_versioned_app, box_model, Pair[str, int], {"pair": {"first": "a", "second": 1}}
    )

    assert clients["2000-01-01"].get("/box").json() == {"pair": {"first": "a", "old_second": 1}}
    assert clients["2001-01-01"].get("/box").json() == {"pair": {"first": "a", "second": 1}}
    old_schemas = clients["unversioned"].get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert [name for name in old_schemas if "Pair" in name] == ["Pair_str_int_"]


@pytest.mark.parametrize("field_shape", ["unhashable_annotated_metadata", "callable_args"])
def test__generate_versioned_models__fields_with_unhashable_parts_reaching_parametrization__should_apply_instructions(
    create_runtime_schemas: Any, field_shape: str
):
    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    if field_shape == "unhashable_annotated_metadata":

        class Box(BaseModel, Generic[_T]):
            pair: Annotated[Pair[_T, int], {"doc": "unhashable metadata"}]

    else:

        class Box(BaseModel, Generic[_T]):
            pair: Pair[_T, int]
            # The args of Callable are a list
            callback: SkipJsonSchema[Optional[Callable[[Pair[_T, int]], None]]] = None

    old_box = create_runtime_schemas(
        version_change(schema(Pair[str, int]).field("second").had(serialization_alias="old_second"))
    )["2000-01-01"][Box[str]]

    assert old_box.model_validate({"pair": {"first": "a", "second": 1}}).model_dump(by_alias=True)["pair"] == {
        "first": "a",
        "old_second": 1,
    }


def test__router_generation__parametrization_with_instructions_reached_through_versioned_args__should_apply_them(
    create_versioned_app: CreateVersionedApp,
):
    class Item(BaseModel):
        name: str

    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    class Box(BaseModel, Generic[_T]):
        pair: Pair[_T, int]

    router = VersionedAPIRouter()

    @router.get("/box", response_model=Box[Item])
    async def get_box() -> dict[str, Any]:
        return {"pair": {"first": {"name": "a"}, "second": 1}}

    app = create_versioned_app(
        version_change(
            schema(Item).field("name").had(serialization_alias="old_name"),
            schema(Pair[Item, int]).field("second").had(serialization_alias="old_second"),
        ),
        router=router,
    )
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert client_2000.get("/box").json() == {"pair": {"first": {"old_name": "a"}, "old_second": 1}}
    assert client_2001.get("/box").json() == {"pair": {"first": {"name": "a"}, "second": 1}}


def test__router_generation__generic_of_parametrization_reaching_instructions__should_apply_only_them(
    create_versioned_app: CreateVersionedApp,
):
    class Kind(str, Enum):
        a = "a"

    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    class Wrapper(BaseModel, Generic[_T]):
        pair: Pair[_T, str]
        pairs: list[Pair[_T, str]] = []

    class Box(BaseModel, Generic[_T]):
        pair: Pair[_T, int]
        wrapper: Wrapper[_T]
        kind: Kind = Kind.a

    class Page(BaseModel, Generic[_T]):
        items: list[_T]

    router = VersionedAPIRouter()
    box = {"pair": {"first": "a", "second": 1}, "wrapper": {"pair": {"first": "b", "second": "c"}, "pairs": []}}

    @router.get("/boxes", response_model=Page[Box[str]])
    async def get_boxes() -> dict[str, Any]:
        return {"items": [box]}

    app = create_versioned_app(
        version_change(schema(Pair[str, int]).field("second").had(serialization_alias="old_second")), router=router
    )
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    old_box = box | {"pair": {"first": "a", "old_second": 1}, "kind": "a"}
    assert client_2000.get("/boxes").json() == {"items": [old_box]}
    assert client_2001.get("/boxes").json() == {"items": [box | {"kind": "a"}]}
    reference_cycles = generate_versioned_models(app.versions)["2000-01-01"]._reference_cycles
    assert reference_cycles.get_redefined_field_names(Box[str]) == ("pair",)
    assert reference_cycles.get_redefined_field_names(Wrapper[str]) == ()


def test__generate_versioned_models__parametrization_with_undefined_names__should_inherit_fields_as_on_main():
    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    class Box(BaseModel, Generic[_T]):
        pair: Pair[_T, int]
        later_pair: "Pair[_T, UndefinedPairArg]"  # noqa: F821 # ty: ignore[unresolved-reference]

    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(Pair[str, int]).field("second").had(serialization_alias="old"))),
        Version("2000-01-01"),
    )
    reference_cycles = generate_versioned_models(versions)["2000-01-01"]._reference_cycles

    # We cannot know what copying the fields of a generic with undefined names would request
    assert reference_cycles.get_redefined_field_names(Box[str]) == ()


class _RecursiveItem(BaseModel):
    name: str


class _RecursivePair(BaseModel, Generic[_T, _U]):
    first: _T
    second: _U


# Pydantic refers to _RecursiveNode[_RecursiveItem] within itself by a reference that does not lead to its copy.
# The models are defined here because Pydantic 2.11 cannot resolve such a model defined within a function.
class _RecursiveNode(BaseModel, Generic[_T]):
    items: "list[Union[_RecursivePair[_T, int], _RecursiveNode[_T]]]" = []


def test__router_generation__parametrization_with_instructions_within_recursive_generic__should_work_as_on_main(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()
    body = {"items": [{"first": {"name": "a"}, "second": 1}, {"items": [{"first": {"name": "b"}, "second": 2}]}]}

    @router.get("/node", response_model=_RecursiveNode[_RecursiveItem])
    async def get_node() -> dict[str, Any]:
        return body

    app = create_versioned_app(
        version_change(
            schema(_RecursiveItem).field("name").had(description="Old name"),
            schema(_RecursivePair[_RecursiveItem, int]).field("second").had(serialization_alias="old_second"),
        ),
        router=router,
    )

    schema_names = []
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/node").json() == body
        schemas = TestClient(app).get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        schema_names.append(sorted(schemas))
    # One schema per model in both versions, as on main
    assert schema_names[0] == schema_names[1]
    assert len([name for name in schema_names[0] if "Pair" in name or "Node" in name]) == 2


def test__generate_versioned_models__parametrizations_that_refer_to_each_other__should_inherit_fields_as_on_main():
    class Pair(BaseModel, Generic[_T, _U]):
        first: _T
        second: _U

    class Limb(BaseModel, Generic[_T]):
        trunk: "Optional[Trunk[_T]]" = None
        pair: Optional[Pair[_T, int]] = None

    limb_model = Limb[str]

    class Trunk(BaseModel, Generic[_T]):
        limb: Optional[Limb[_T]] = None

    class Stump(BaseModel, Generic[_T]):
        limb: Optional[Limb[_T]] = None

    trunk_model = Trunk[str]
    Limb.model_rebuild()
    limb_model.model_rebuild()
    # Pydantic builds them separately so they refer to each other by the classes themselves. If their copies defined
    # the fields that pydantic otherwise builds itself, copying them would never finish
    assert trunk_model.model_fields["limb"].annotation == Optional[limb_model]
    assert limb_model.model_fields["trunk"].annotation == Optional[trunk_model]

    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(Pair[str, int]).field("second").had(serialization_alias="old"))),
        Version("2000-01-01"),
    )
    reference_cycles = generate_versioned_models(versions)["2000-01-01"]._reference_cycles

    assert reference_cycles.get_redefined_field_names(trunk_model) == ()
    assert reference_cycles.get_redefined_field_names(limb_model) == ()
    # Stump[str] only leads to the cycle so it is not in it
    assert reference_cycles.get_redefined_field_names(Stump[str]) == ("limb",)
