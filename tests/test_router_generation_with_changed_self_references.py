"""Models in reference cycles that need versioning, such as a self-referencing model with instructions (#419).

The unchanged ones are reused as they are (see test_router_generation_with_self_references.py).
"""

import functools
import re
import threading
import typing
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Generic, Literal, Optional, TypeVar, Union

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    ValidationError,
    computed_field,
    field_serializer,
)
from starlette.status import HTTP_200_OK, HTTP_422_UNPROCESSABLE_CONTENT
from typing_extensions import override

from cadwyn import (
    Cadwyn,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    generate_versioned_models,
    migrate_response_body,
)
from cadwyn.exceptions import CadwynError, SchemaGenerationError
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema
from tests.conftest import CreateVersionedApp, version_change

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal as OnlyForTypeCheckers

ValueT = TypeVar("ValueT")


class Category(BaseModel):
    name: str
    children: "list[Category]" = []


class SpecialCategory(Category):
    special: bool = True


class Author(BaseModel):
    name: str
    books: "list[Book]" = []


class Book(BaseModel):
    name: str
    author: Optional[Author] = None


class OptionalCategory(BaseModel):
    name: str
    parent: "Optional[OptionalCategory]" = None


class DictCategory(BaseModel):
    name: str
    children: "dict[str, DictCategory]" = {}


class UnionCategory(BaseModel):
    name: str
    child: "Union[UnionCategory, int]" = 0


class PipeUnionCategory(BaseModel):
    name: str
    child: "PipeUnionCategory | None" = None


class AnnotatedCategory(BaseModel):
    name: str
    children: "Annotated[list[AnnotatedCategory], Field(max_length=2)]" = []


class Cat(BaseModel):
    kind: Literal["cat"] = "cat"
    name: str
    friends: "list[Annotated[Union[Cat, Dog], Field(discriminator='kind')]]" = []


class Dog(BaseModel):
    kind: Literal["dog"] = "dog"
    name: str
    owner: Optional[Cat] = None


class BackReferencingBase(BaseModel):
    name: str
    sub: "Optional[BackReferencingSub]" = None


class BackReferencingSub(BackReferencingBase):
    pass


class TreeNode(BaseModel, Generic[ValueT]):
    name: str
    value: ValueT
    children: "list[TreeNode[ValueT]]" = []


class Page(BaseModel, Generic[ValueT]):
    items: list[ValueT]


class Envelope(BaseModel, Generic[ValueT]):
    item: ValueT


class Node(BaseModel, Generic[ValueT]):
    name: str
    value: ValueT
    parent: "Optional[IntNode]" = None


IntNode = Node[int]
Node.model_rebuild()
IntNode.model_rebuild()


class PagedCategory(BaseModel):
    name: str
    children: "Optional[Page[PagedCategory]]" = None


class Leaf(BaseModel):
    name: str


class LazyItem(BaseModel):
    name: str


class LazyTree(BaseModel):
    item: LazyItem
    children: "list[LazyTree]" = []


class LazyShelf(BaseModel):
    name: str
    trees: Page[LazyTree]


class LazyWebhook(BaseModel):
    tree: LazyTree
    related: Page[LazyTree]


class VersionedParent(BaseModel):
    name: str


class SelfReferencingChild(VersionedParent):
    children: "list[SelfReferencingChild]" = []


class DefaultChild(BaseModel):
    name: str = "child"
    parent: "Optional[DefaultParent]" = None


class DefaultParent(BaseModel):
    name: str = "parent"
    child: DefaultChild = Field(default_factory=DefaultChild)


class DeferredCategory(BaseModel):
    model_config = ConfigDict(defer_build=True)
    name: str
    children: "list[DeferredCategory]" = []


class ChangedItem(BaseModel):
    name: str


class Note(BaseModel):
    text: str


def make_aliases():  # Cadwyn cannot know what default factories without annotations return
    return []


class ItemTree(BaseModel):
    item: ChangedItem
    note: Optional[Note] = None
    tags: tuple[str, ...] = ("tag", "tag")
    aliases: list[str] = Field(default_factory=make_aliases)
    children: "list[ItemTree]" = []

    @computed_field
    @property
    def child_count(self) -> int:
        return len(self.children)


class Registry(BaseModel):
    name: str
    entry: "Optional[RegistryEntry]" = None


class RegistryEntry(BaseModel):
    name: str
    registry: Optional[Registry] = None
    default_registry: ClassVar[Optional[Registry]] = None
    _cached_registry: Optional[Registry] = PrivateAttr(default=None)
    on_change: ClassVar[Optional[Callable[[Registry], None]]] = None


class NestedStringCategory(BaseModel):
    name: str
    children: list["NestedStringCategory"] = []


VALIDATION_CALLS: list[str] = []


def count_validation(value: Any) -> Any:
    VALIDATION_CALLS.append("called")
    return value


class ValidatedCategory(BaseModel):
    name: str
    children: "Annotated[list[ValidatedCategory], AfterValidator(count_validation)]" = []


def short_name(model: type[BaseModel]):
    return schema(model).field("name").had(max_length=3)


def described_name(model: type[BaseModel]):
    return schema(model).field("name").had(description="Old name")


def get_schema_names(client: TestClient, version: str) -> list[str]:
    response = client.get(f"/openapi.json?version={version}")
    assert response.status_code == HTTP_200_OK
    return list(response.json()["components"]["schemas"])


def assert_no_duplicate_schemas(schema_names: list[str]) -> None:
    # Pydantic disambiguates the different classes with the same name by their full paths and numbers
    assert not [name for name in schema_names if name.startswith("tests__") or re.search(r"__\d+$", name)]


# The model of the route, the changed model (whose names are at most 3 characters long in the old version) or
# the instruction, a body that is valid in every version, and a body that only the old version rejects
CHANGED_CYCLE_CASES = [
    pytest.param(
        Category,
        Category,
        {"name": "abc", "children": [{"name": "abc", "children": []}]},
        {"name": "abc", "children": [{"name": "toolong", "children": []}]},
        id="list",
    ),
    pytest.param(
        Author,
        Book,
        {"name": "abcdef", "books": [{"name": "abc", "author": {"name": "abcdef", "books": []}}]},
        {"name": "abcdef", "books": [{"name": "toolong", "author": None}]},
        id="mutual",
    ),
    pytest.param(
        OptionalCategory,
        OptionalCategory,
        {"name": "abc", "parent": {"name": "abc", "parent": None}},
        {"name": "abc", "parent": {"name": "toolong", "parent": None}},
        id="optional",
    ),
    pytest.param(
        DictCategory,
        DictCategory,
        {"name": "abc", "children": {"a": {"name": "abc", "children": {}}}},
        {"name": "abc", "children": {"a": {"name": "toolong", "children": {}}}},
        id="dict",
    ),
    pytest.param(
        UnionCategory,
        UnionCategory,
        {"name": "abc", "child": {"name": "abc", "child": 1}},
        {"name": "abc", "child": {"name": "toolong", "child": 1}},
        id="union",
    ),
    pytest.param(
        PipeUnionCategory,
        PipeUnionCategory,
        {"name": "abc", "child": {"name": "abc", "child": None}},
        {"name": "abc", "child": {"name": "toolong", "child": None}},
        id="pipe_union",
    ),
    pytest.param(
        AnnotatedCategory,
        AnnotatedCategory,
        {"name": "abc", "children": [{"name": "abc", "children": []}]},
        {"name": "abc", "children": [{"name": "toolong", "children": []}]},
        id="annotated",
    ),
    pytest.param(
        Cat,
        Dog,
        {"kind": "cat", "name": "abc", "friends": [{"kind": "dog", "name": "abc", "owner": None}]},
        {"kind": "cat", "name": "abc", "friends": [{"kind": "dog", "name": "toolong", "owner": None}]},
        id="discriminated_union",
    ),
    pytest.param(
        BackReferencingBase,
        BackReferencingBase,
        {"name": "abc", "sub": {"name": "abc", "sub": None}},
        {"name": "abc", "sub": {"name": "toolong", "sub": None}},
        id="changed_base_with_back_reference",
    ),
    pytest.param(
        BackReferencingBase,
        schema(BackReferencingSub).field("extra").existed_as(type=int, info=Field(default=0)),
        {"name": "a", "sub": {"name": "b", "sub": {"name": "c", "sub": None, "extra": 0}, "extra": 0}},
        {"name": "a", "sub": {"name": "b", "sub": {"name": "c", "sub": None, "extra": "x"}, "extra": 0}},
        id="changed_sub_with_back_reference",
    ),
    pytest.param(
        SpecialCategory,
        Category,
        {"name": "abc", "special": True, "children": [{"name": "abc", "children": []}]},
        {"name": "abc", "special": True, "children": [{"name": "toolong", "children": []}]},
        id="subclass_of_changed_self_referencing_model",
    ),
    pytest.param(
        TreeNode[int],
        TreeNode,
        {"name": "abc", "value": 1, "children": [{"name": "abc", "value": 2, "children": []}]},
        {"name": "abc", "value": 1, "children": [{"name": "toolong", "value": 2, "children": []}]},
        id="generic",
    ),
    pytest.param(
        SelfReferencingChild,
        VersionedParent,
        {"name": "abc", "children": [{"name": "abc", "children": []}]},
        {"name": "abc", "children": [{"name": "toolong", "children": []}]},
        id="self_referencing_child_of_changed_parent",
    ),
    pytest.param(
        Node[str],
        Node,
        {"name": "abc", "value": "a", "parent": {"name": "abc", "value": 1, "parent": None}},
        {"name": "abc", "value": "a", "parent": {"name": "toolong", "value": 1, "parent": None}},
        id="concrete_parametrization_in_cycle",
    ),
    pytest.param(
        DefaultParent,
        DefaultChild,
        {"name": "parent", "child": {"name": "abc", "parent": {"name": "x", "child": {"name": "abc", "parent": None}}}},
        {"name": "parent", "child": {"name": "abc", "parent": {"name": "x", "child": {"name": "toolong"}}}},
        id="default_factory_in_cycle",
    ),
    pytest.param(
        Registry,
        RegistryEntry,
        {"name": "abcdef", "entry": {"name": "abc", "registry": {"name": "abcdef", "entry": None}}},
        {"name": "abcdef", "entry": {"name": "abc", "registry": {"name": "x", "entry": {"name": "toolong"}}}},
        id="class_var_and_private_attribute",
    ),
]


@pytest.mark.parametrize(("model", "change", "valid_body", "invalid_old_body"), CHANGED_CYCLE_CASES)
def test__router_generation__changed_models_in_reference_cycles__should_be_versioned_at_every_depth(
    model: type[BaseModel],
    change: Any,
    valid_body: dict[str, Any],
    invalid_old_body: dict[str, Any],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/models", response_model=model)
    async def create_model(payload: model) -> Any:  # ty: ignore[invalid-type-form]
        return payload

    shortens_names = isinstance(change, type)
    app = create_versioned_app(version_change(short_name(change) if shortens_names else change), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    new_client = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    assert old_client.post("/models", json=valid_body).json() == valid_body
    assert new_client.post("/models", json=invalid_old_body).status_code == HTTP_200_OK
    response = old_client.post("/models", json=invalid_old_body)
    assert response.status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["detail"][0]["type"] == ("string_too_long" if shortens_names else "int_parsing")

    for version in ("2000-01-01", "2001-01-01"):
        assert_no_duplicate_schemas(get_schema_names(old_client, version))
    if not shortens_names:
        return
    old_openapi = old_client.get("/openapi.json?version=2000-01-01").json()
    old_schemas = [
        definition
        for definition in old_openapi["components"]["schemas"].values()
        if definition.get("properties", {}).get("name", {}).get("maxLength") == 3
    ]
    assert old_schemas


def rename_name_to_title(data: Any) -> None:
    data["title"] = data.pop("name")
    for child in data.get("children", []):
        rename_name_to_title(child)


def rename_title_to_name(data: Any) -> None:
    data["name"] = data.pop("title")
    for child in data.get("children", []):
        rename_title_to_name(child)


def test__issue_419_reproduction__renamed_field_of_self_referencing_model__should_be_renamed_at_every_depth(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/categories", response_model=Category)
    async def get_categories() -> dict[str, Any]:
        return {"name": "root", "children": [{"name": "leaf"}]}

    @router.post("/categories", response_model=Category)
    async def create_category(category: Category) -> Category:
        return category

    @convert_request_to_next_version_for(Category)
    def rename_request(request: RequestInfo) -> None:
        rename_title_to_name(request.body)

    @convert_response_to_previous_version_for(Category)
    def rename_response(response: ResponseInfo) -> None:
        rename_name_to_title(response.body)

    app = create_versioned_app(
        version_change(
            schema(Category).field("name").had(name="title"),
            rename_request=rename_request,
            rename_response=rename_response,
        ),
        router=router,
    )
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    new_client = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    old_body = {"title": "root", "children": [{"title": "leaf", "children": []}]}
    new_body = {"name": "root", "children": [{"name": "leaf", "children": []}]}
    assert old_client.get("/categories").json() == old_body
    assert new_client.get("/categories").json() == new_body
    assert old_client.post("/categories", json=old_body).json() == old_body
    assert new_client.post("/categories", json=new_body).json() == new_body
    assert old_client.post("/categories", json=new_body).status_code == HTTP_422_UNPROCESSABLE_CONTENT

    old_category = generate_versioned_models(app.versions)["2000-01-01"][Category]
    assert old_category is not Category
    assert set(old_category.model_fields) == {"title", "children"}
    assert old_category.model_fields["children"].annotation == list[old_category]
    # The copies refer to each other by their classes, not by the names that Cadwyn resolves them with
    assert typing.get_type_hints(old_category) == {"title": str, "children": list[old_category]}
    old_openapi = old_client.get("/openapi.json?version=2000-01-01").json()
    old_response = old_openapi["paths"]["/categories"]["get"]["responses"]["200"]["content"]["application/json"]
    old_schema = old_openapi["components"]["schemas"][old_response["schema"]["$ref"].rsplit("/", 1)[-1]]
    assert set(old_schema["properties"]) == {"title", "children"}
    assert old_schema["properties"]["children"]["items"] == old_response["schema"]
    changelog = TestClient(app).get("/changelog")
    assert changelog.status_code == HTTP_200_OK
    assert "title" in changelog.text


def test__generate_versioned_models__copies_in_cycle__should_refer_to_each_other():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(Book))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    old_author, old_book = generator[Author], generator[Book]
    assert old_author is not Author
    assert old_book is not Book
    assert old_author.model_fields["books"].annotation == list[old_book]
    assert old_book.model_fields["author"].annotation == Optional[old_author]
    assert typing.get_type_hints(old_author) == {"name": str, "books": list[old_book]}

    old_entry = generate_versioned_models(
        VersionBundle(
            Version("2001-01-01", version_change(short_name(RegistryEntry))),
            Version("2000-01-01"),
        )
    )["2000-01-01"][RegistryEntry]
    old_registry = typing.get_args(old_entry.model_fields["registry"].annotation)[0]
    assert old_registry is not Registry
    assert typing.get_type_hints(old_entry) == {
        "name": str,
        "registry": Optional[old_registry],
        "default_registry": ClassVar[Optional[old_registry]],
        "_cached_registry": Optional[old_registry],
        "on_change": ClassVar[Optional[Callable[[old_registry], None]]],
    }


def test__router_generation__validator_on_field_in_cycle__should_run_once(create_versioned_app: CreateVersionedApp):
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=ValidatedCategory)
    async def create_category(category: ValidatedCategory) -> ValidatedCategory:
        return category

    app = create_versioned_app(version_change(short_name(ValidatedCategory)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    old_category = generate_versioned_models(app.versions)["2000-01-01"][ValidatedCategory]

    VALIDATION_CALLS.clear()
    old_category.model_validate({"name": "abc", "children": []})
    assert VALIDATION_CALLS == ["called"]
    assert old_client.post("/categories", json={"name": "abc", "children": []}).status_code == HTTP_200_OK


def get_openapi_components(app: Any, version: str) -> dict[str, Any]:
    response = TestClient(app).get(f"/openapi.json?version={version}")
    assert response.status_code == HTTP_200_OK
    return response.json()["components"]["schemas"]


def get_request_body_schema(openapi_components: dict[str, Any], openapi: dict[str, Any], path: str) -> Any:
    reference = openapi["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    return openapi_components[reference.rsplit("/", 1)[-1]]


@pytest.mark.parametrize("generic_route_first", [False, True])
@pytest.mark.parametrize(
    ("model", "generic_model", "valid_body", "invalid_old_body"),
    [
        pytest.param(
            TreeNode[int],
            Page[TreeNode[int]],
            {"name": "abc", "value": 1, "children": [{"name": "abc", "value": 2, "children": []}]},
            {"name": "abc", "value": 1, "children": [{"name": "toolong", "value": 2, "children": []}]},
            id="generic_tree",
        ),
        pytest.param(
            Category,
            Page[Category],
            {"name": "abc", "children": [{"name": "abc", "children": []}]},
            {"name": "abc", "children": [{"name": "toolong", "children": []}]},
            id="self_referencing_model",
        ),
    ],
)
def test__router_generation__generic_route_model_around_changed_cycle__should_use_versioned_args(
    model: Any,
    generic_model: Any,
    valid_body: dict[str, Any],
    invalid_old_body: dict[str, Any],
    generic_route_first: bool,
    create_versioned_app: CreateVersionedApp,
):
    """The copies of a cycle that has instructions always exist, so the generics around it use them too"""
    router = VersionedAPIRouter()

    def add_model_route():
        @router.post("/model", response_model=model)
        async def create_model(payload: model) -> Any:
            return payload

    def add_generic_route():
        @router.post("/page", response_model=generic_model)
        async def create_page(page: generic_model) -> Any:
            return page

    if generic_route_first:
        add_generic_route()
        add_model_route()
    else:
        add_model_route()
        add_generic_route()

    app = create_versioned_app(version_change(short_name(get_origin_of_model(model))), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    for path, body, invalid_body in [
        ("/model", valid_body, invalid_old_body),
        ("/page", {"items": [valid_body]}, {"items": [invalid_old_body]}),
    ]:
        assert old_client.post(path, json=body).json() == body
        response = old_client.post(path, json=invalid_body)
        assert response.status_code == HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["detail"][0]["type"] == "string_too_long"

    components = get_openapi_components(app, "2000-01-01")
    assert_no_duplicate_schemas(list(components))
    old_openapi = TestClient(app).get("/openapi.json?version=2000-01-01").json()
    model_schema = get_request_body_schema(components, old_openapi, "/model")
    assert model_schema["properties"]["name"]["maxLength"] == 3
    page_schema = get_request_body_schema(components, old_openapi, "/page")
    assert page_schema["properties"]["items"]["items"] == model_schema["properties"]["children"]["items"]
    generator = generate_versioned_models(app.versions)["2000-01-01"]
    assert generator[Envelope[model]].model_fields["item"].annotation is generator[model]


class ItemOfPagedCategory(BaseModel):
    name: str


class CategoryWithItem(BaseModel):
    name: str
    item: Optional[ItemOfPagedCategory] = None
    children: "list[CategoryWithItem]" = []


class LatestCategoryPageHolder(BaseModel):
    @computed_field
    @property
    def categories(self) -> Page[CategoryWithItem]:
        raise NotImplementedError


def test__router_generation__computed_field_with_latest_generic_around_cycle__should_raise(
    create_versioned_app: CreateVersionedApp,
):
    """Pydantic builds the latest Page[CategoryWithItem] on its own, not Cadwyn's copy"""
    router = VersionedAPIRouter()

    @router.post("/category", response_model=CategoryWithItem)
    async def create_category(category: CategoryWithItem) -> Any:
        raise NotImplementedError

    @router.get("/holder", response_model=LatestCategoryPageHolder)
    async def get_holder() -> Any:
        raise NotImplementedError

    with pytest.raises(
        SchemaGenerationError, match="copy of LatestCategoryPageHolder because it would use the latest CategoryWithItem"
    ):
        create_versioned_app(version_change(short_name(ItemOfPagedCategory)), router=router)


class ItemOfTag(BaseModel):
    name: str


class TagWithItem(BaseModel):
    label: str
    item: Optional[ItemOfTag] = None
    children: "list[TagWithItem]" = []


class CategoryColor(str, Enum):
    red = "red"


class CategoryWithTag(BaseModel):
    name: str
    tag: Optional[TagWithItem] = None
    color: CategoryColor = CategoryColor.red
    children: "list[CategoryWithTag]" = []


@pytest.mark.parametrize(
    "tag_change",
    [
        lambda field: field.didnt_exist,
        lambda field: field.had(type=Optional[str]),
        lambda field: field.had(description="Old"),
    ],
    ids=["didnt_exist", "had_type", "had_description"],
)
def test__router_generation__generic_arg_whose_copy_drops_reference_to_cycle__should_work(
    tag_change: Callable[[Any], Any],
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/categories", response_model=Page[CategoryWithTag])
    async def get_categories() -> Any:
        raise NotImplementedError

    @router.get("/tag", response_model=TagWithItem)
    async def get_tag() -> Any:
        raise NotImplementedError

    @router.get("/category", response_model=CategoryWithTag)
    async def get_category() -> Any:
        raise NotImplementedError

    app = create_versioned_app(
        version_change(short_name(ItemOfTag), tag_change(schema(CategoryWithTag).field("tag"))), router=router
    )
    assert TestClient(app).get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK


class ValueBox(BaseModel, Generic[ValueT]):
    value: ValueT


class BoxOverridingTypeArgField(ValueBox[int]):
    # Overrides the field typed by the type arg, so CategoryWithItem does not come from a type arg
    value: list["CategoryWithItem"]  # ty: ignore[invalid-attribute-override]


class BoxWithSerializerOfTypeArgField(ValueBox[int]):
    @field_serializer("value", return_type=CategoryWithItem)
    def serialize_value(self, value: int) -> Any:
        raise NotImplementedError


class OtherBox(BaseModel, Generic[ValueT]):
    other: ValueT


class BoxWithSerializerOfOtherBaseTypeArgField(ValueBox[int], OtherBox[CategoryWithItem]):
    # `value` comes from ValueBox[int], so CategoryWithItem from OtherBox[CategoryWithItem] is not its type arg
    @field_serializer("value", return_type=CategoryWithItem)
    def serialize_value(self, value: int) -> Any:
        raise NotImplementedError


@pytest.mark.parametrize("box_route_first", [False, True])
@pytest.mark.parametrize(
    "box_model",
    [BoxOverridingTypeArgField, BoxWithSerializerOfTypeArgField, BoxWithSerializerOfOtherBaseTypeArgField],
    ids=["overridden_field", "serializer", "serializer_with_type_arg_of_other_base"],
)
def test__router_generation__latest_model_of_cycle_in_field_inherited_from_generic__should_raise(
    box_model: type[BaseModel],
    box_route_first: bool,
    create_versioned_app: CreateVersionedApp,
):
    """Pydantic resolves the latest models in overridden fields and in serializers of inherited fields on its own"""
    router = VersionedAPIRouter()

    def add_category_route() -> None:
        @router.get("/category", response_model=CategoryWithItem)
        async def get_category() -> Any:
            raise NotImplementedError

    def add_box_route() -> None:
        @router.get("/box", response_model=box_model)
        async def get_box() -> Any:
            raise NotImplementedError

    for add_route in (add_box_route, add_category_route) if box_route_first else (add_category_route, add_box_route):
        add_route()

    with pytest.raises(
        SchemaGenerationError, match=f"copy of {box_model.__name__} because it would use the latest CategoryWithItem"
    ):
        create_versioned_app(version_change(short_name(ItemOfPagedCategory)), router=router)


class GenericCategoryList(RootModel[list[ValueT]], Generic[ValueT]):
    pass


class CategoryList(RootModel[list[CategoryWithItem]]):
    pass


@pytest.mark.parametrize(
    "root_model", [GenericCategoryList[CategoryWithItem], CategoryList], ids=["generic_root_model", "root_model"]
)
def test__router_generation__root_model_with_type_arg_around_changed_cycle__should_version_it(
    root_model: Any,
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/category", response_model=CategoryWithItem)
    async def create_category(category: CategoryWithItem) -> Any:
        return category

    @router.post("/categories", response_model=root_model)
    async def create_categories(categories: root_model) -> Any:
        return categories

    app = create_versioned_app(version_change(short_name(ItemOfPagedCategory)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    old_category = {"name": "a", "item": {"name": "abc"}, "children": []}
    latest_category = {"name": "a", "item": {"name": "toolong"}, "children": []}

    assert old_client.post("/category", json=old_category).json() == old_category
    assert old_client.post("/categories", json=[old_category]).json() == [old_category]
    assert old_client.post("/categories", json=[latest_category]).status_code == HTTP_422_UNPROCESSABLE_CONTENT


def get_origin_of_model(model: Any) -> Any:
    return model.__pydantic_generic_metadata__["origin"] or model


def test__router_generation__generic_route_model_around_renamed_cycle__should_apply_its_converter(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/pages", response_model=Page[Category])
    async def get_page() -> dict[str, Any]:
        return {"items": [{"name": "root", "children": [{"name": "leaf"}]}]}

    @convert_response_to_previous_version_for(Page[Category])
    def rename_page(response: ResponseInfo) -> None:
        for item in response.body["items"]:
            rename_name_to_title(item)

    app = create_versioned_app(
        version_change(schema(Category).field("name").had(name="title"), rename_page=rename_page), router=router
    )
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert old_client.get("/pages").json() == {
        "items": [{"title": "root", "children": [{"title": "leaf", "children": []}]}]
    }


def test__router_generation__generic_args_that_lead_back_to_the_generic__should_keep_head_args(
    create_versioned_app: CreateVersionedApp,
):
    """Cadwyn keeps the latest args of generics whose args lead back to the generic itself, as it always did"""
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=PagedCategory)
    async def create_category(category: PagedCategory) -> Any:
        return category

    app = create_versioned_app(version_change(short_name(PagedCategory)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert old_client.post("/categories", json={"name": "toolong"}).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    nested_body = {"name": "abc", "children": {"items": [{"name": "toolong", "children": None}]}}
    assert old_client.post("/categories", json=nested_body).json() == nested_body
    # The changelog generates another copy of PagedCategory whose generic args refer to the version's copy
    assert TestClient(app).get("/changelog").status_code == HTTP_200_OK


@pytest.mark.parametrize("generic_first", [False, True])
def test__router_generation__generic_around_cycle_that_no_route_copies__should_keep_head_args(
    generic_first: bool, create_versioned_app: CreateVersionedApp
):
    """The cycle refers to a changed model, so it needs versioning, but no route copies it, so it works as before.

    Copying it later (e.g. in migrate_response_body, which failed on main) does not change the generics around it.
    """
    router = VersionedAPIRouter()

    @router.get("/pages", response_model=Page[LazyTree])
    async def get_page() -> dict[str, Any]:
        return {"items": [{"item": {"name": "x"}}]}

    app = create_versioned_app(version_change(described_name(LazyItem)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert old_client.get("/pages").json() == {"items": [{"item": {"name": "x"}, "children": []}]}
    components = get_openapi_components(app, "2000-01-01")
    assert components["LazyItem"]["properties"]["name"] == {"type": "string", "title": "Name"}

    def migrate_tree() -> None:
        old_tree = migrate_response_body(
            app.versions, LazyTree, latest_body={"item": {"name": "x"}}, version="2000-01-01"
        )
        assert type(old_tree) is generate_versioned_models(app.versions)["2000-01-01"][LazyTree]
        assert type(old_tree) is not LazyTree

    def migrate_page() -> None:
        old_page = migrate_response_body(
            app.versions, Page[LazyTree], latest_body={"items": [{"item": {"name": "x"}}]}, version="2000-01-01"
        )
        assert type(old_page.items[0]) is LazyTree

    migrations = [migrate_tree, migrate_page]
    for migrate in reversed(migrations) if generic_first else migrations:
        migrate()
    assert old_client.get("/pages").json() == {"items": [{"item": {"name": "x"}, "children": []}]}


def test__router_generation__generic_that_kept_head_args_before_routes_that_do_not_use_its_cycle__should_work():
    versions = VersionBundle(
        Version("2001-01-01", version_change(described_name(LazyItem))),
        Version("2000-01-01"),
    )
    old_page = migrate_response_body(
        versions, Page[LazyTree], latest_body={"items": [{"item": {"name": "x"}}]}, version="2000-01-01"
    )
    router = VersionedAPIRouter()

    @router.get("/notes", response_model=Note)
    async def get_note() -> dict[str, Any]:
        return {"text": "x"}

    app = Cadwyn(versions=versions)
    app.generate_and_include_versioned_routers(router)
    client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert type(old_page.items[0]) is LazyTree
    assert client.get("/notes").json() == {"text": "x"}


@pytest.mark.parametrize("generic_route_first", [False, True])
def test__router_generation__generic_around_cycle_that_a_route_copies__should_use_versioned_args(
    generic_route_first: bool, create_versioned_app: CreateVersionedApp
):
    router = VersionedAPIRouter()

    def add_tree_route():
        @router.post("/tree", response_model=LazyTree)
        async def create_tree(tree: LazyTree) -> Any:
            return tree

    def add_page_route():
        @router.post("/pages", response_model=Page[LazyTree])
        async def create_page(page: Page[LazyTree]) -> Any:
            return page

    if generic_route_first:
        add_page_route()
        add_tree_route()
    else:
        add_tree_route()
        add_page_route()

    app = create_versioned_app(version_change(short_name(LazyItem)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    tree = {"item": {"name": "abc"}, "children": [{"item": {"name": "abc"}, "children": []}]}
    invalid_tree = {"item": {"name": "abc"}, "children": [{"item": {"name": "toolong"}, "children": []}]}
    assert old_client.post("/tree", json=tree).json() == tree
    assert old_client.post("/pages", json={"items": [tree]}).json() == {"items": [tree]}
    assert old_client.post("/tree", json=invalid_tree).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert old_client.post("/pages", json={"items": [invalid_tree]}).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert_no_duplicate_schemas(list(get_openapi_components(app, "2000-01-01")))


def get_one() -> int:
    return 1


def test__router_generation__route_with_unresolvable_dependency_annotation__should_still_copy_cycle_eagerly(
    create_versioned_app: CreateVersionedApp,
):
    """Recording the routes resolves the annotations of endpoints and dependencies one by one, like copying them"""
    router = VersionedAPIRouter()

    @router.post("/tree")
    async def create_tree(
        tree: LazyTree,
        unresolvable: "OnlyForTypeCheckers" = Depends(get_one),
    ) -> dict[str, bool]:
        return {"ok": True}

    @router.post("/pages")
    async def create_page(page: Page[LazyTree]) -> dict[str, bool]:
        return {"ok": True}

    # The newest version versions the head routes, whose annotations are the hardest to resolve
    app = create_versioned_app(head_version_changes=[version_change(short_name(LazyItem))], router=router)
    client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    invalid_tree = {"item": {"name": "abc"}, "children": [{"item": {"name": "toolong"}, "children": []}]}
    assert client.post("/tree", json=invalid_tree).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert client.post("/pages", json={"items": [invalid_tree]}).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    tree = {"item": {"name": "abc"}, "children": []}
    assert client.post("/tree", json=tree).json() == {"ok": True}
    assert client.post("/pages", json={"items": [tree]}).json() == {"ok": True}


def test__router_generation__generic_that_keeps_head_args_around_cycle_that_a_route_copies__should_raise(
    create_versioned_app: CreateVersionedApp,
):
    """The changed shelf is copied before Cadwyn knows the routes, so its generic keeps the latest LazyTree"""
    router = VersionedAPIRouter()

    @router.get("/tree", response_model=LazyTree)
    async def get_tree() -> dict[str, Any]:
        raise NotImplementedError

    with pytest.raises(SchemaGenerationError, match=r"LazyTree.* the latest models in the type args of generics"):
        create_versioned_app(version_change(short_name(LazyItem), short_name(LazyShelf)), router=router)


def test__router_generation__generic_that_keeps_head_args_around_cycle_that_a_route_copies__should_raise_every_time():
    """A failed attempt to start the app must not register the routes, or the next attempt would skip the check"""
    router = VersionedAPIRouter()

    @router.get("/tree", response_model=LazyTree)
    async def get_tree() -> dict[str, Any]:
        raise NotImplementedError

    app = Cadwyn(
        versions=VersionBundle(
            Version("2001-01-01", version_change(short_name(LazyItem), short_name(LazyShelf))),
            Version("2000-01-01"),
        )
    )
    app.generate_and_include_versioned_routers(router)

    for _ in range(3):
        with pytest.raises(SchemaGenerationError, match=r"LazyTree.* the latest models in the type args of generics"):
            app._cadwyn_initialize()


def test__router_generation__self_referencing_model_changed_in_head_version__should_be_versioned_in_every_version(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=Category)
    async def create_category(category: Category) -> Category:
        return category

    app = create_versioned_app(
        version_change(short_name(Leaf)), head_version_changes=[version_change(short_name(Category))], router=router
    )

    nested_body = {"name": "abc", "children": [{"name": "abc", "children": [{"name": "toolong", "children": []}]}]}
    valid_body = {"name": "abc", "children": [{"name": "abc", "children": []}]}
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/categories", json=nested_body).status_code == HTTP_422_UNPROCESSABLE_CONTENT
        assert client.post("/categories", json=valid_body).json() == valid_body


def test__generate_versioned_models__cycle_that_only_exists_in_older_versions__should_be_copied():
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(schema(Leaf).field("parent").existed_as(type=Optional[Leaf], info=Field(default=None))),
        ),
        Version("2000-01-01"),
    )
    generators = generate_versioned_models(versions)

    old_leaf = generators["2000-01-01"][Leaf]
    assert old_leaf.model_fields["parent"].annotation == Optional[old_leaf]  # ty: ignore[invalid-type-form]
    leaf: Any = old_leaf.model_validate({"name": "a", "parent": {"name": "b", "parent": {"name": "c"}}})
    assert type(leaf.parent) is old_leaf
    assert type(leaf.parent.parent) is old_leaf
    assert "parent" not in generators["2001-01-01"][Leaf].model_fields


def test__generate_versioned_models__cycle_that_does_not_exist_in_older_versions__should_be_copied_as_before():
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(Category).field("children").didnt_exist)),
        Version("2000-01-01"),
    )
    generators = generate_versioned_models(versions)

    assert list(generators["2000-01-01"][Category].model_fields) == ["name"]
    new_category = generators["2001-01-01"][Category]
    assert new_category is not Category
    assert new_category.model_fields["children"].annotation == list[new_category]


class DocumentedCategory(BaseModel):
    """INTERNAL: head-only implementation note."""

    name: str
    children: "list[DocumentedCategory]" = []


def test__router_generation__description_of_self_referencing_model_changed__should_change_it_at_every_depth():
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=DocumentedCategory)
    async def create_category(category: DocumentedCategory) -> DocumentedCategory:
        return category

    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(DocumentedCategory).had(description="A category."))),
        Version("2000-01-01"),
    )
    app = Cadwyn(versions=versions, changelog_url=None)
    app.generate_and_include_versioned_routers(router)
    client = TestClient(app)

    old_schemas = client.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    new_schemas = client.get("/openapi.json?version=2001-01-01").json()["components"]["schemas"]
    # FastAPI separates the request and the response schemas because "children" has a default
    for name in ("DocumentedCategory-Input", "DocumentedCategory-Output"):
        assert old_schemas[name]["description"] == "A category."
        assert old_schemas[name]["properties"]["children"]["items"] == {"$ref": f"#/components/schemas/{name}"}
        assert new_schemas[name]["description"] == "INTERNAL: head-only implementation note."
    old_category = generate_versioned_models(versions)["2000-01-01"][DocumentedCategory]
    assert old_category.model_fields["children"].annotation == list[old_category]  # ty: ignore[invalid-type-form]
    body = {"name": "a", "children": [{"name": "b", "children": []}]}
    assert client.post("/categories", json=body, headers={"x-api-version": "2000-01-01"}).json() == body


def test__migrate_response_body__changed_self_referencing_model__should_be_validated_with_old_model():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(Category))),
        Version("2000-01-01"),
    )

    body = {"name": "abc", "children": [{"name": "abc", "children": []}]}
    migrated = migrate_response_body(versions, Category, latest_body=body, version="2000-01-01")
    assert type(migrated) is generate_versioned_models(versions)["2000-01-01"][Category]
    assert type(migrated.children[0]) is type(migrated)
    with pytest.raises(ValueError, match="at most 3 characters"):
        migrate_response_body(
            versions, Category, latest_body={"name": "abc", "children": [{"name": "toolong"}]}, version="2000-01-01"
        )


def test__migrate_response_body__changed_self_referencing_model_with_deferred_build__should_be_built():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(DeferredCategory))),
        Version("2000-01-01"),
    )

    body = {"name": "abc", "children": [{"name": "abc", "children": []}]}
    migrated = migrate_response_body(versions, DeferredCategory, latest_body=body, version="2000-01-01")
    assert migrated.model_dump() == body
    with pytest.raises(ValueError, match="at most 3 characters"):
        migrate_response_body(
            versions,
            DeferredCategory,
            latest_body={"name": "abc", "children": [{"name": "toolong"}]},
            version="2000-01-01",
        )


def test__router_generation__request_migration_for_changed_self_referencing_model__should_apply(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=Category)
    async def create_category(category: Category) -> Category:
        return category

    @convert_request_to_next_version_for(Category)
    def uppercase_name(request: RequestInfo) -> None:
        request.body["name"] = request.body["name"].upper()

    app = create_versioned_app(version_change(short_name(Category), uppercase_name=uppercase_name), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    body = {"name": "abc", "children": [{"name": "b", "children": []}]}
    assert old_client.post("/categories", json=body).json() == {"name": "ABC", "children": body["children"]}


def test__generate_versioned_models__cycle_copied_from_several_threads__should_be_copied_once():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(ChangedItem))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]
    thread_count = 8
    barrier = threading.Barrier(thread_count)
    results: list[Any] = []

    def copy_item_tree():
        barrier.wait()
        results.append(generator[ItemTree])

    threads = [threading.Thread(target=copy_item_tree) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == thread_count
    assert len({id(result) for result in results}) == 1
    old_item_tree = results[0]
    assert old_item_tree.model_fields["children"].annotation == list[old_item_tree]
    assert old_item_tree.model_fields["item"].annotation is generator[ChangedItem]
    assert typing.get_args(old_item_tree.model_fields["note"].annotation)[0] is generator[Note]
    tree = old_item_tree.model_validate({"item": {"name": "abc"}, "children": [{"item": {"name": "abc"}}]})
    assert tree.model_dump()["child_count"] == 1


def test__router_generation__nested_quoted_self_reference__should_work_as_before(
    create_versioned_app: CreateVersionedApp,
):
    """Reference discovery does not see `list["Model"]` but pydantic resolves the name to the copy itself"""
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=NestedStringCategory)
    async def create_category(category: NestedStringCategory) -> Any:
        return category

    app = create_versioned_app(version_change(short_name(NestedStringCategory)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    for invalid_body in [
        {"name": "toolong"},
        {"name": "abc", "children": [{"name": "toolong"}]},
        {"name": "abc", "children": [{"name": "abc", "children": [{"name": "toolong"}]}]},
    ]:
        assert old_client.post("/categories", json=invalid_body).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    valid_body = {"name": "abc", "children": [{"name": "abc", "children": []}]}
    assert old_client.post("/categories", json=valid_body).json() == valid_body


# Shapes that Cadwyn cannot copy


class Box:
    """A custom generic whose subscription requires real classes"""

    def __class_getitem__(cls, item: Any) -> Any:
        return typing._GenericAlias(cls, (item,))  # ty: ignore[unresolved-attribute]

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.list_schema(handler.generate_schema(typing.get_args(source)[0]))


class BoxedCategory(BaseModel):
    name: str
    children: "Box[BoxedCategory]" = []  # ty: ignore[invalid-type-form]


BoxedCategory.model_rebuild()


class MixedCategory(BaseModel):
    name: str
    parent: "Optional[MixedCategory]" = None
    children: "Optional[Page[MixedCategory]]" = None


class QuotedAuthor(BaseModel):
    name: str
    books: "list[QuotedBook]" = []
    favorite: Optional["QuotedBook"] = None


class QuotedBook(BaseModel):
    name: str
    author: "Optional[QuotedAuthor]" = None


QuotedAuthor.model_rebuild()


class HiddenCategory(BaseModel):
    name: str
    children: "list[HiddenCategory]" = []
    books: list["HiddenBook"] = []


class HiddenBook(BaseModel):
    category: HiddenCategory


HiddenCategory.model_rebuild()


class SerializedBook(BaseModel):
    category: "Optional[SerializedCategory]" = None


class SerializedCategory(BaseModel):
    name: str
    books: list[SerializedBook] = []

    @field_serializer("books")
    def serialize_books(self, books: Any) -> list[SerializedBook]:
        return books  # pragma: no cover


SerializedBook.model_rebuild()


class QuotingHolder(BaseModel):
    unchanged: Optional[DictCategory] = None
    categories: list["Category"] = []


class Settings(BaseModel):
    name: str = "default"


def make_settings() -> Settings:
    return Settings()  # pragma: no cover


class SettingsNode(BaseModel):
    settings: Settings = Settings()
    children: "list[SettingsNode]" = []


class SettingsFactoryNode(BaseModel):
    settings: Settings = Field(default_factory=make_settings)
    children: "list[SettingsFactoryNode]" = []


class SettingsPartialNode(BaseModel):
    settings: Settings = Field(default_factory=functools.partial(functools.partial(Settings, name="partial")))
    children: "list[SettingsPartialNode]" = []


class SettingsConstructNode(BaseModel):
    settings: Settings = Field(default_factory=Settings.model_construct)
    children: "list[SettingsConstructNode]" = []


class DefaultCategoryHolder(BaseModel):
    category: Category = Category(name="abc")


class PartialCategoryHolder(BaseModel):
    category: Category = Field(default_factory=functools.partial(Category, name="abc"))


class SettingsLambdaNode(BaseModel):
    settings: Settings = Field(default_factory=lambda: Settings(name="default"))
    children: "list[SettingsLambdaNode]" = []


class CategoryBox(BaseModel):
    category: Category


class BoxedDefaultHolder(BaseModel):
    box: CategoryBox = CategoryBox(category=Category(name="abc"))


def make_category(data: "Mapping[str, object]") -> Category:
    return Category(name="abc")  # pragma: no cover


class UnreadableFactoryHolder(BaseModel):
    category: Category = Field(default_factory=make_category)


class Status(str, Enum):
    active = "active"
    new = "new"


class StatusNode(BaseModel):
    status: Status = Status.new
    children: "list[StatusNode]" = []


class SafeFactoriesCategory(BaseModel):
    name: str
    children: "list[SafeFactoriesCategory]" = Field(default_factory=list)
    by_name: "dict[str, SafeFactoriesCategory]" = Field(default_factory=functools.partial(dict, {}))
    settings: Settings = Field(default_factory=Settings)
    unrelated: list[str] = Field(default_factory=lambda: ["unrelated"])
    optional_settings: list[Optional[Settings]] = [None, None]


class UnrelatedOrder(BaseModel):
    status: Status = Status.new
    # Cadwyn cannot know what the lambda returns but this field does not use the cycle
    items: list[ChangedItem] = Field(default_factory=lambda: [])  # noqa: PIE807


class ClassmethodDefaults(BaseModel):
    @classmethod
    def make_settings(cls) -> Settings:
        return Settings(name="abc")  # pragma: no cover


class ClassmethodNode(BaseModel):
    settings: Settings = Field(default_factory=ClassmethodDefaults.make_settings)
    children: "list[ClassmethodNode]" = []


class OverridingConstructorDefaults(BaseModel):
    @classmethod
    @override
    def model_validate(cls, *args: Any, **kwargs: Any) -> Any:
        return Settings(name="abc")  # pragma: no cover


class OverridingConstructorNode(BaseModel):
    settings: Settings = Field(default_factory=OverridingConstructorDefaults.model_validate)
    children: "list[OverridingConstructorNode]" = []


class UnchangedConstructed(BaseModel):
    pass


class CallOverridingPartial(functools.partial):
    @override
    def __call__(self, /, *args: Any, **kwargs: Any) -> Any:
        return Settings()  # pragma: no cover


class PartialSubclassNode(BaseModel):
    settings: Settings = Field(default_factory=CallOverridingPartial(UnchangedConstructed.model_construct))
    children: "list[PartialSubclassNode]" = []


class ConstructorOverride(BaseModel):
    @classmethod
    @override
    def model_construct(cls, *args: Any, **kwargs: Any) -> Any:
        return Settings()  # pragma: no cover


class RootConstructorFactory(RootModel[int], ConstructorOverride):
    pass


class InheritedConstructorNode(BaseModel):
    settings: Settings = Field(  # ty: ignore[invalid-assignment] # The constructor is overridden to return Settings
        default_factory=functools.partial(RootConstructorFactory.model_construct, 1)
    )
    children: "list[InheritedConstructorNode]" = []


class UntypedDefaultHolder(BaseModel):
    # Cadwyn cannot know what the lambda returns and the field does not use the cycle, as for any other model
    meta: Any = Field(default_factory=lambda: None)
    raw: dict[str, Any] = {"category": Category(name="abc")}


class LambdaChildrenNode(BaseModel):
    name: str
    children: "list[LambdaChildrenNode]" = Field(default_factory=lambda: [])  # noqa: PIE807


class CategoryRoot(RootModel[list[Category]]):
    pass


class UnbuildableHolder(BaseModel):
    category: Category
    unbuildable: "NotDefinedAnywhere"  # noqa: F821 # ty: ignore[unresolved-reference]


class DeferredEnvelope(BaseModel):
    model_config = ConfigDict(defer_build=True)
    categories: list["Category"]


class RetroFeed(BaseModel):
    name: str
    trees: list["LazyTree"] = []
    category: Optional[OptionalCategory] = None


class ComputedCategory(BaseModel):
    name: str
    children: "list[ComputedCategory]" = []

    @computed_field
    @property
    def first_child(self) -> "Optional[ComputedCategory]":
        return self.children[0] if self.children else None  # pragma: no cover


def make_children() -> "list[UndefinedCategory]":  # noqa: F821 # ty: ignore[unresolved-reference]
    return []  # pragma: no cover


class IncompleteCategory(BaseModel):
    name: str
    children: "list[IncompleteCategory]" = Field(default_factory=make_children)


@pytest.mark.parametrize(
    ("model", "error_message"),
    [
        pytest.param(
            BoxedCategory,
            "BoxedCategory.* refer to each other in positions that need the classes themselves",
            id="custom_generic",
        ),
        pytest.param(ComputedCategory, "would use the latest ComputedCategory", id="computed_field"),
        pytest.param(SerializedCategory, "would use the latest .*SerializedBook", id="serializer"),
        pytest.param(QuotedBook, "would use the latest .*QuotedBook", id="partially_quoted"),
        pytest.param(HiddenCategory, "would use the latest HiddenBook", id="quoted_arg"),
        pytest.param(
            LambdaChildrenNode,
            "cannot verify what the default factory of the field 'children' of LambdaChildrenNode returns.*"
            "default_factory=list",
            id="lambda_default_factory",
        ),
        pytest.param(IncompleteCategory, "IncompleteCategory.* not defined yet", id="undefined_names"),
    ],
)
def test__generate_versioned_models__cycle_that_cannot_be_copied__should_raise(
    model: type[BaseModel], error_message: str
):
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(model))),
        Version("2000-01-01"),
    )

    with pytest.raises(SchemaGenerationError, match=error_message):
        generate_versioned_models(versions)


def test__generate_versioned_models__cycle_with_generic_field_whose_args_lead_back__should_raise():
    """The generic within the cycle keeps the latest MixedCategory in its args because they lead back to the cycle"""
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(MixedCategory))),
        Version("2000-01-01"),
    )

    with pytest.raises(SchemaGenerationError, match="would use the latest MixedCategory"):
        generate_versioned_models(versions)


@pytest.mark.parametrize(
    ("changed_model", "requested_model", "error_message"),
    [
        pytest.param(
            Category,
            QuotingHolder,
            "copy of QuotingHolder because it would use the latest Category",
            id="quoted_name_outside",
        ),
        pytest.param(
            Settings, SettingsNode, "default of the field 'settings' of SettingsNode contains an instance", id="default"
        ),
        pytest.param(
            Settings,
            SettingsFactoryNode,
            "cannot verify what the default factory of the field 'settings' of SettingsFactoryNode returns",
            id="default_factory",
        ),
        pytest.param(
            Settings,
            SettingsPartialNode,
            "cannot verify what the default factory of the field 'settings' of SettingsPartialNode returns",
            id="partial_default_factory",
        ),
        pytest.param(
            Settings,
            SettingsConstructNode,
            "cannot verify what the default factory of the field 'settings' of SettingsConstructNode returns",
            id="classmethod_default_factory",
        ),
        pytest.param(
            Category,
            DefaultCategoryHolder,
            "default of the field 'category' of DefaultCategoryHolder contains an instance",
            id="default_outside",
        ),
        pytest.param(
            Category,
            PartialCategoryHolder,
            "cannot verify what the default factory of the field 'category' of PartialCategoryHolder returns",
            id="partial_default_factory_outside",
        ),
        pytest.param(
            Settings,
            ClassmethodNode,
            "cannot verify what the default factory of the field 'settings' of ClassmethodNode returns",
            id="classmethod_of_other_model",
        ),
        pytest.param(
            Settings,
            OverridingConstructorNode,
            "cannot verify what the default factory of the field 'settings' of OverridingConstructorNode returns",
            id="overridden_constructor",
        ),
        pytest.param(
            Settings,
            PartialSubclassNode,
            "cannot verify what the default factory of the field 'settings' of PartialSubclassNode returns",
            id="partial_subclass",
        ),
        pytest.param(
            Settings,
            InheritedConstructorNode,
            "cannot verify what the default factory of the field 'settings' of InheritedConstructorNode returns",
            id="constructor_overridden_in_base",
        ),
        pytest.param(
            Category,
            UntypedDefaultHolder,
            "default of the field 'raw' of UntypedDefaultHolder contains an instance of the latest Category",
            id="untyped_field_default",
        ),
        pytest.param(
            Settings,
            SettingsLambdaNode,
            "cannot verify what the default factory of the field 'settings' of SettingsLambdaNode returns",
            id="lambda_default_factory",
        ),
        pytest.param(
            Category,
            BoxedDefaultHolder,
            "default of the field 'box' of BoxedDefaultHolder contains an instance of the latest Category",
            id="nested_default",
        ),
        pytest.param(
            Category,
            UnreadableFactoryHolder,
            "cannot verify what the default factory of the field 'category' of UnreadableFactoryHolder returns",
            id="default_factory_with_unreadable_annotations",
        ),
        pytest.param(
            Category,
            DeferredEnvelope,
            "copy of DeferredEnvelope because it would use the latest Category",
            id="deferred",
        ),
    ],
)
def test__generate_versioned_models__copy_that_would_use_latest_models_of_cycle__should_raise(
    changed_model: type[BaseModel],
    requested_model: type[BaseModel],
    error_message: str,
):
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(changed_model))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    with pytest.raises(SchemaGenerationError, match=error_message):
        generator[requested_model]


def test__generate_versioned_models__copy_with_generic_that_keeps_latest_models_of_cycle__should_raise():
    """The generic keeps the latest LazyTree because the cycle is copied lazily, next to a field with its copy"""
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(LazyItem))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    with pytest.raises(SchemaGenerationError, match="copy of LazyWebhook because it would use the latest LazyTree"):
        generator[LazyWebhook]


def test__generate_versioned_models__enum_default_in_cycle__should_work_as_for_other_models():
    """Enum members are serialized by their values, so Cadwyn uses them as defaults as it always did"""
    versions = VersionBundle(
        Version("2001-01-01", version_change(enum(Status).didnt_have("new"))),
        Version("2000-01-01"),
    )
    old_node = generate_versioned_models(versions)["2000-01-01"][StatusNode]

    # As for any other model with such a default, Cadwyn keeps the latest enum member
    assert old_node.model_fields["status"].default is Status.new
    assert old_node().children == []


def test__generate_versioned_models__defaults_of_fields_that_do_not_reach_copied_cycles__should_work_as_on_main():
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(short_name(Category), short_name(ChangedItem), enum(Status).didnt_have("new")),
        ),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    assert generator[Category] is not Category
    old_order = generator[UnrelatedOrder]
    assert old_order.model_fields["status"].default is Status.new
    assert old_order().items == []


def test__generate_versioned_models__default_factories_that_create_versioned_copies__should_be_allowed():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(Settings), short_name(SafeFactoriesCategory))),
        Version("2000-01-01"),
    )
    old_category = generate_versioned_models(versions)["2000-01-01"][SafeFactoriesCategory]

    category = old_category(name="abc")
    assert category.children == []
    assert category.by_name == {}
    old_settings = old_category.model_fields["settings"].annotation
    assert type(category.settings) is old_settings
    assert category.unrelated == ["unrelated"]
    assert category.optional_settings == [None, None]


def test__generate_versioned_models__root_model_around_changed_cycle__should_use_its_copies():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(Category))),
        Version("2000-01-01"),
    )
    old_root = generate_versioned_models(versions)["2000-01-01"][CategoryRoot]

    assert old_root.model_validate([{"name": "abc"}]).model_dump() == [{"name": "abc", "children": []}]
    with pytest.raises(ValueError, match="at most 3 characters"):
        old_root.model_validate([{"name": "toolong"}])


def test__generate_versioned_models__copy_that_cannot_be_built_in_version_that_copies_cycles__should_raise():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(Category))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    with pytest.raises(SchemaGenerationError, match="cannot check the versioned copy of UnbuildableHolder"):
        generator[UnbuildableHolder]


def test__router_generation__copy_checked_before_a_route_cycle_is_copied__should_be_checked_again(
    create_versioned_app: CreateVersionedApp,
):
    """RetroFeed is copied with OptionalCategory before Cadwyn knows that a route copies LazyTree"""
    router = VersionedAPIRouter()

    @router.post("/feeds")
    async def create_feed(feed: RetroFeed) -> dict[str, bool]:
        raise NotImplementedError

    @router.post("/trees")
    async def create_tree(tree: LazyTree) -> dict[str, bool]:
        raise NotImplementedError

    with pytest.raises(SchemaGenerationError, match="copy of RetroFeed because it would use the latest LazyTree"):
        create_versioned_app(
            version_change(short_name(LazyItem), short_name(RetroFeed), short_name(OptionalCategory)), router=router
        )


class ItemOfLatestCategory(BaseModel):
    name: str


class CategoryOfComputedField(BaseModel):
    name: str
    item: Optional[ItemOfLatestCategory] = None
    parent: "Optional[CategoryOfComputedField]" = None


class ComputedCategoryHolder(BaseModel):
    @computed_field
    @property
    def category(self) -> CategoryOfComputedField:
        raise NotImplementedError


@pytest.mark.parametrize("holder_route_first", [True, False], ids=["holder_route_first", "cycle_route_first"])
def test__router_generation__copy_that_fails_its_check_after_its_cycle_is_copied__should_fail_on_every_attempt(
    holder_route_first: bool,
):
    """Copying the cycle checks the copies generated before it, so a failed check must not leave the cycle copied"""
    router = VersionedAPIRouter()

    def add_holder_route() -> None:
        @router.get("/holder", response_model=ComputedCategoryHolder)
        async def get_holder() -> Any:
            raise NotImplementedError

    def add_category_route() -> None:
        @router.get("/category", response_model=CategoryOfComputedField)
        async def get_category() -> Any:
            raise NotImplementedError

    for add_route in (
        (add_holder_route, add_category_route) if holder_route_first else (add_category_route, add_holder_route)
    ):
        add_route()
    app = Cadwyn(
        versions=VersionBundle(
            Version("2001-01-01", version_change(short_name(ItemOfLatestCategory))),
            Version("2000-01-01"),
        )
    )
    app.generate_and_include_versioned_routers(router)

    # Each request initializes the app again until it succeeds, with the versioned models that the bundle cached
    for _ in range(3):
        with pytest.raises(
            SchemaGenerationError,
            match="copy of ComputedCategoryHolder because it would use the latest CategoryOfComputedField",
        ):
            TestClient(app).get("/holder", headers={app.router.api_version_parameter_name: "2000-01-01"})


# The generators that HookedItemTree asks for its copy while pydantic creates its subclasses
HOOK_GENERATORS: list[Any] = []


class AsksForItsCopies:
    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)  # ty: ignore[unresolved-attribute]
        for generator in HOOK_GENERATORS:
            generator[HookedItemTree]


class HookedItemTree(AsksForItsCopies, BaseModel):
    item: ChangedItem
    children: "list[HookedItemTree]" = []


def test__generate_versioned_models__hook_that_asks_for_copy_while_its_cycle_is_copied__should_raise():
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(ChangedItem))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]
    HOOK_GENERATORS.append(generator)
    try:
        with pytest.raises(CadwynError, match="HookedItemTree was requested from outside of its reference cycle"):
            generator[HookedItemTree]
    finally:
        HOOK_GENERATORS.clear()


def test__generate_versioned_models__schema_hook_that_reads_cached_copies_from_another_thread__should_work():
    """Rebuilding a model must not wait for Cadwyn's lookups of the copies that it has already generated"""
    versions = VersionBundle(
        Version("2001-01-01", version_change(short_name(ChangedItem))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]
    old_item = generator[ChangedItem]
    worker_results: list[Any] = []

    class ReadsCachedCopiesInSchemaHook(BaseModel):
        model_config = ConfigDict(defer_build=True)

        @classmethod
        @override
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
            worker = threading.Thread(target=lambda: worker_results.append(generator[ChangedItem]), daemon=True)
            worker.start()
            worker.join(5)
            return handler(source)

    ReadsCachedCopiesInSchemaHook.model_rebuild()

    assert worker_results == [old_item]


class NodeWithInheritedName(BaseModel):
    name: str
    children: "list[NodeWithInheritedName]" = []


class ShortNamedNode(NodeWithInheritedName):
    child: "Optional[ShortNamedNode]" = None


class OtherNode(NodeWithInheritedName):
    pass


class CategoryOfSubcategories(BaseModel):
    title: str
    subcategories: "list[Subcategory]" = []


class Subcategory(CategoryOfSubcategories):
    pass


class OtherSubcategory(CategoryOfSubcategories):
    pass


class GenericTree(BaseModel, Generic[ValueT]):
    value: ValueT
    label: str = ""
    children: "list[GenericTree[ValueT]]" = []


class IntTree(GenericTree[int]):
    pass


class OtherIntTree(GenericTree[int]):
    pass


def test__router_generation__subclass_of_self_referencing_model_changed_inherited_field__should_not_change_others(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/nodes", response_model=NodeWithInheritedName)
    async def create_node(node: NodeWithInheritedName) -> NodeWithInheritedName:
        return node

    @router.post("/short_named_nodes", response_model=ShortNamedNode)
    async def create_short_named_node(node: ShortNamedNode) -> ShortNamedNode:
        return node

    @router.post("/other_nodes", response_model=OtherNode)
    async def create_other_node(node: OtherNode) -> OtherNode:
        return node

    app = create_versioned_app(
        version_change(
            schema(NodeWithInheritedName).field("children").had(description="Children"),
            schema(ShortNamedNode).field("name").had(max_length=3),
        ),
        router=router,
    )
    client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    long_names = {"name": "long", "children": [{"name": "longer", "children": []}]}
    assert client.post("/nodes", json=long_names).json() == long_names
    assert client.post("/other_nodes", json=long_names).json() == long_names
    # The children of the subclass are parents, so they don't have the change
    short_named = {"name": "abc", "children": [{"name": "longer", "children": []}], "child": None}
    assert client.post("/short_named_nodes", json=short_named).json() == short_named
    for invalid_body in ({**short_named, "name": "long"}, {**short_named, "child": {"name": "long"}}):
        response = client.post("/short_named_nodes", json=invalid_body)
        assert response.status_code == HTTP_422_UNPROCESSABLE_CONTENT
    schemas = client.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert schemas["ShortNamedNode-Input"]["properties"]["name"]["maxLength"] == 3
    assert "maxLength" not in schemas["NodeWithInheritedName-Input"]["properties"]["name"]
    assert "maxLength" not in schemas["OtherNode-Input"]["properties"]["name"]


def test__generate_versioned_models__subclass_in_cycle_with_parent_changed_inherited_field__should_change_only_it():
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                schema(CategoryOfSubcategories).field("subcategories").had(description="Subcategories"),
                schema(Subcategory).field("title").had(max_length=3),
            ),
        ),
        Version("2000-01-01"),
    )
    old_models = generate_versioned_models(versions)["2000-01-01"]

    old_category = old_models[CategoryOfSubcategories]
    old_subcategory = old_models[Subcategory]
    assert old_category.model_fields["subcategories"].annotation == list[old_subcategory]  # ty: ignore[invalid-type-form]
    assert old_category.model_validate({"title": "long", "subcategories": [{"title": "abc"}]}).title == "long"
    with pytest.raises(ValidationError):
        old_category.model_validate({"title": "long", "subcategories": [{"title": "long"}]})
    assert old_models[OtherSubcategory].model_validate({"title": "long"}).title == "long"


def test__generate_versioned_models__subclass_of_self_referencing_parametrization_changed_inherited_field__should_not_change_others():  # noqa: E501
    versions = VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                schema(GenericTree).field("value").had(description="Value"),
                schema(IntTree).field("label").had(max_length=3),
            ),
        ),
        Version("2000-01-01"),
    )
    old_models = generate_versioned_models(versions)["2000-01-01"]

    with pytest.raises(ValidationError):
        old_models[IntTree].model_validate({"value": 1, "label": "long"})
    # The children of IntTree are parametrizations of its parent, so they don't have the change
    tree: Any = old_models[IntTree].model_validate(
        {"value": 1, "label": "abc", "children": [{"value": 2, "label": "long"}]}
    )
    assert tree.children[0].label == "long"
    for model in (GenericTree, GenericTree[int], OtherIntTree):
        assert old_models[model].model_validate({"value": 1, "label": "long"}).label == "long"


def test__generate_versioned_models__self_referencing_parent_renamed_field_that_subclass_changed__should_keep_change():
    versions = VersionBundle(
        Version(
            "2002-01-01",
            version_change(schema(ShortNamedNode).field("children").had(description="Short named children")),
        ),
        Version("2001-01-01", version_change(schema(NodeWithInheritedName).field("children").had(name="kids"))),
        Version("2000-01-01"),
    )
    old_models = generate_versioned_models(versions)["2000-01-01"]

    old_node = old_models[NodeWithInheritedName]
    old_short_named_node = old_models[ShortNamedNode]
    assert old_node.model_fields["kids"].description is None
    assert old_models[OtherNode].model_fields["kids"].description is None
    assert old_short_named_node.model_fields["kids"].description == "Short named children"
    assert old_short_named_node.model_fields["kids"].annotation == list[old_node]  # ty: ignore[invalid-type-form]
    node: Any = old_short_named_node.model_validate({"name": "a", "kids": [{"name": "b", "kids": [{"name": "c"}]}]})
    assert type(node.kids[0].kids[0]) is old_node
