from enum import Enum
from typing import Any, Generic, Optional, TypeVar, Union, get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, Field, SerializeAsAny, ValidationError
from starlette.status import HTTP_200_OK, HTTP_422_UNPROCESSABLE_CONTENT

from cadwyn import (
    Cadwyn,
    HeadVersion,
    RequestInfo,
    Version,
    VersionBundle,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    generate_versioned_models,
    migrate_response_body,
)
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema
from tests.conftest import CreateVersionedApp, version_change

ValueT = TypeVar("ValueT")


class Item(BaseModel):
    name: str = Field(alias="fullName")


class Category(BaseModel):
    name: str
    children: "list[Category]" = []


class OptionalCategory(BaseModel):
    name: str
    parent: "Optional[OptionalCategory]" = None


class DictCategory(BaseModel):
    name: str
    children: "dict[str, DictCategory]" = {}


class UnionCategory(BaseModel):
    name: str
    child: "Union[UnionCategory, int, None]" = None


class Author(BaseModel):
    name: str
    books: "list[Book]" = []


class Book(BaseModel):
    name: str
    author: Optional[Author] = None


class BackReferencingBase(BaseModel):
    sub: "Optional[BackReferencingSub]" = None


class BackReferencingSub(BackReferencingBase):
    pass


class TreeNode(BaseModel, Generic[ValueT]):
    value: ValueT
    children: "list[TreeNode[ValueT]]" = []


class IntTreeNode(TreeNode[int]):
    pass


# The generators that HookedTree asks about itself while pydantic creates its subclasses, with their answers
HOOKED_TREE_GENERATORS: list[tuple[Any, list[Any]]] = []


class HookedTree(BaseModel, Generic[ValueT]):
    value: Optional[ValueT] = None
    children: "list[HookedTree[ValueT]]" = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for generator, answers in HOOKED_TREE_GENERATORS:
            answers.append(generator[HookedTree])


class Color(str, Enum):
    red = "red"
    green = "green"


class ColoredCategory(BaseModel):
    name: str
    color: Color = Color.red
    children: "list[ColoredCategory]" = []


class ColoredItem(BaseModel):
    color: Color = Color.red


class ItemWithCategory(BaseModel):
    name: str = Field(alias="fullName")
    category: Optional[Category] = None


class CategoryParent(BaseModel):
    name: str = Field(alias="fullName")


class CategoryChild(CategoryParent):
    children: "list[Category]" = []


class OuterOfItem(BaseModel):
    item: Item


class TaggedParent(BaseModel):
    def __init_subclass__(cls, *, tag: str, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)


class TaggedChild(TaggedParent, tag="child"):
    x: int


class StrippingParent(BaseModel):
    x: str

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.x = self.x.strip()


class StrippingChild(StrippingParent):
    x: str


class Animal(BaseModel):
    name: str


class Dog(Animal):
    is_good: bool


class AnimalEnvelope(BaseModel):
    value: SerializeAsAny[Animal]


class AnyModelEnvelope(BaseModel):
    # Like on main, the fields that accept any model are never versioned
    value: SerializeAsAny[BaseModel]


class SharedBase(BaseModel):
    pass


class ChangedSibling(SharedBase):
    name: str


class SiblingCategory(SharedBase):
    name: str
    children: "list[SiblingCategory]" = []


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


def unrelated_version_change():
    return version_change(old_name_alias(Item))


SELF_REFERENCING_MODELS_AND_BODIES = [
    pytest.param(Category, {"name": "root", "children": [{"name": "leaf", "children": []}]}, id="list"),
    pytest.param(OptionalCategory, {"name": "leaf", "parent": {"name": "root", "parent": None}}, id="optional"),
    pytest.param(DictCategory, {"name": "root", "children": {"a": {"name": "leaf", "children": {}}}}, id="dict"),
    pytest.param(UnionCategory, {"name": "root", "child": {"name": "leaf", "child": 1}}, id="union"),
    pytest.param(Author, {"name": "a", "books": [{"name": "b", "author": {"name": "a", "books": []}}]}, id="mutual"),
    pytest.param(TreeNode[int], {"value": 1, "children": [{"value": 2, "children": []}]}, id="generic"),
    pytest.param(ColoredCategory, {"name": "a", "color": "green", "children": []}, id="with_enum"),
]


@pytest.mark.parametrize(("model", "body"), SELF_REFERENCING_MODELS_AND_BODIES)
@pytest.mark.parametrize("has_older_version", [False, True], ids=["single_version", "with_unrelated_change"])
def test__router_generation__unchanged_self_referencing_models__should_work_in_every_version(
    model: type[BaseModel],
    body: dict[str, Any],
    has_older_version: bool,
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/models", response_model=model)
    async def get_model() -> dict[str, Any]:
        return body

    @router.post("/models", response_model=model)
    async def create_model(payload: model) -> Any:  # ty: ignore[invalid-type-form]
        return payload

    app = create_versioned_app(*([unrelated_version_change()] if has_older_version else []), router=router)
    unversioned_client = TestClient(app)
    for version in app.router.versioned_routers:
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/models").json() == body
        assert client.post("/models", json=body).json() == body
        assert client.post("/models", json={"children": 1}).status_code == HTTP_422_UNPROCESSABLE_CONTENT
        openapi = unversioned_client.get(f"/openapi.json?version={version}")
        assert openapi.status_code == HTTP_200_OK
        # The unchanged models are the same classes in every version so they never get duplicated
        schema_names = openapi.json()["components"]["schemas"]
        assert not [name for name in schema_names if name.endswith(("__1", "__2"))]


def test__issue_419_reproduction__should_serve_self_referencing_model():
    app = Cadwyn(versions=VersionBundle(Version("2000-01-01")))
    router = VersionedAPIRouter()

    @router.get("/categories", response_model=Category)
    async def get_categories() -> dict[str, Any]:
        return {"name": "root", "children": [{"name": "leaf"}]}

    app.generate_and_include_versioned_routers(router)
    client = TestClient(app, headers={"x-api-version": "2000-01-01"})

    assert client.get("/categories").json() == {"name": "root", "children": [{"name": "leaf", "children": []}]}
    assert client.get("/openapi.json?version=2000-01-01").status_code == HTTP_200_OK


@pytest.mark.parametrize("has_instructions", [False, True])
def test__generate_versioned_models__unchanged_models_in_reference_cycles__should_be_the_head_classes(
    has_instructions: bool,
):
    versions = VersionBundle(
        Version("2001-01-01", *([unrelated_version_change()] if has_instructions else [])),
        Version("2000-01-01"),
    )

    for generator in generate_versioned_models(versions).values():
        for model in (
            Category,
            Author,
            Book,
            TreeNode,
            ColoredCategory,
            BackReferencingBase,
            BackReferencingSub,
            Color,
        ):
            assert generator[model] is model
            assert not hasattr(generator[model], "__cadwyn_original_model__")


@pytest.mark.parametrize("has_instructions", [False, True])
def test__generate_versioned_models__unchanged_models_outside_of_reference_cycles__should_be_copied_as_before(
    has_instructions: bool,
):
    versions = VersionBundle(
        Version("2001-01-01", *([unrelated_version_change()] if has_instructions else [])),
        Version("2000-01-01"),
    )

    for generator in generate_versioned_models(versions).values():
        for model in (ItemWithCategory, CategoryChild, TreeNode[int], IntTreeNode, AnyModelEnvelope, OuterOfItem, Item):
            assert generator[model] is not model
        # Their copies refer to the head models in reference cycles
        assert generator[ItemWithCategory].model_fields["category"].annotation == Optional[Category]
        assert generator[TreeNode[int]].__bases__ == (TreeNode,)
        assert generator[OuterOfItem].model_fields["item"].annotation is generator[Item]


def test__generate_versioned_models__models_referring_to_changed_models__should_still_be_versioned():
    versions = VersionBundle(
        Version("2001-01-01", version_change(old_name_alias(CategoryParent))),
        Version("2000-01-01"),
    )
    generators = generate_versioned_models(versions)

    # A child of a changed model, even if its own fields refer to unchanged models only
    old_child = generators["2000-01-01"][CategoryChild]
    assert old_child is not CategoryChild
    assert old_child.__bases__ == (generators["2000-01-01"][CategoryParent],)
    assert old_child.model_fields["name"].alias == "oldName"
    # The unchanged self-referencing model that it refers to is the head one
    assert old_child.model_fields["children"].annotation == list[Category]
    assert generators["2001-01-01"][CategoryChild].model_fields["name"].alias == "fullName"


def test__generate_versioned_models__model_changed_only_in_head_version__should_be_versioned():
    versions = VersionBundle(
        HeadVersion(version_change(schema(Item).field("name").had(description="Head description"))),
        Version("2001-01-01"),
        Version("2000-01-01"),
    )

    for generator in generate_versioned_models(versions).values():
        assert generator[Item] is not Item
        assert generator[Item].model_fields["name"].description == "Head description"
        assert generator[OuterOfItem].model_fields["item"].annotation is generator[Item]
        assert generator[Category] is Category


def test__generate_versioned_models__changed_enum_in_unchanged_model__should_version_the_model():
    versions = VersionBundle(
        Version("2001-01-01", version_change(enum(Color).didnt_have("green"))),
        Version("2000-01-01"),
    )
    generators = generate_versioned_models(versions)

    old_item = generators["2000-01-01"][ColoredItem]
    assert old_item is not ColoredItem
    assert old_item.model_validate({"color": "red"}).model_dump(mode="json") == {"color": "red"}
    with pytest.raises(ValidationError):
        old_item.model_validate({"color": "green"})
    assert generators["2000-01-01"][Category] is Category


def test__router_generation__changed_model_referring_to_unchanged_self_referencing_model__should_work(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()
    body = {"fullName": "a", "category": {"name": "root", "children": [{"name": "leaf", "children": []}]}}

    @router.post("/items", response_model=ItemWithCategory)
    async def create_item(item: ItemWithCategory) -> ItemWithCategory:
        return item

    app = create_versioned_app(
        version_change(schema(ItemWithCategory).field("name").had(description="Old name")), router=router
    )
    unversioned_client = TestClient(app)

    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/items", json=body).json() == body
        schemas = unversioned_client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        assert sorted(schemas) == [
            "Category-Input",
            "Category-Output",
            "HTTPValidationError",
            "ItemWithCategory-Input",
            "ItemWithCategory-Output",
            "ValidationError",
        ]


def test__router_generation__request_migration_for_unchanged_self_referencing_model__should_apply(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=Category)
    async def create_category(category: Category) -> Category:
        return category

    @convert_request_to_next_version_for(Category)
    def uppercase_name(request: RequestInfo) -> None:
        request.body["name"] = request.body["name"].upper()

    app = create_versioned_app(version_change(old_name_alias(Item), uppercase_name=uppercase_name), router=router)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})

    body = {"name": "a", "children": [{"name": "b", "children": []}]}
    assert client_2000.post("/categories", json=body).json() == {"name": "A", "children": body["children"]}
    assert client_2001.post("/categories", json=body).json() == body


@pytest.mark.parametrize("version", ["2000-01-01", "2001-01-01"])
def test__migrate_response_body__unchanged_self_referencing_model__should_be_validated(version: str):
    versions = VersionBundle(
        Version("2001-01-01", unrelated_version_change()),
        Version("2000-01-01"),
    )

    migrated_body = migrate_response_body(
        versions,
        Category,
        latest_body={"name": "root", "children": [Category(name="leaf")]},
        version=version,
    )

    assert type(migrated_body) is Category
    assert migrated_body.model_dump() == {"name": "root", "children": [{"name": "leaf", "children": []}]}


class Wrapper(BaseModel, Generic[ValueT]):
    items: list[ValueT]


class CategoryWithItemPage(BaseModel):
    name: str
    items: Optional[Wrapper[Item]] = None
    children: "list[CategoryWithItemPage]" = []


def test__generate_versioned_models__self_referencing_model_with_changed_generic_arg__should_be_copied():
    versions = VersionBundle(
        Version("2001-01-01", version_change(old_name_alias(Item))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    old_category = generator[CategoryWithItemPage]
    # Its generic uses the old Item so it needs a copy of its own
    assert old_category is not CategoryWithItemPage
    old_page = get_args(old_category.model_fields["items"].annotation)[0]
    assert get_args(old_page.model_fields["items"].annotation) == (generator[Item],)


def test__generate_versioned_models__generic_arg_referring_to_self_referencing_model__should_be_versioned():
    versions = VersionBundle(
        Version("2001-01-01", version_change(old_name_alias(ItemWithCategory))),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]

    # The self-referencing Category no longer stops the args from being versioned because it is not copied
    assert get_args(generator[Wrapper[ItemWithCategory]].model_fields["items"].annotation) == (
        generator[ItemWithCategory],
    )
    assert generator[ItemWithCategory].model_fields["category"].annotation == Optional[Category]


def test__router_generation__changed_child_of_parent_with_init_subclass_arguments__should_work(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.get("/children", response_model=TaggedChild)
    async def get_child() -> dict[str, Any]:
        return {"x": 1}

    app = create_versioned_app(
        version_change(schema(TaggedChild).field("x").had(description="Old description")), router=router
    )
    unversioned_client = TestClient(app)

    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.get("/children").json() == {"x": 1}
        assert unversioned_client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK


def test__generate_versioned_models__changed_child_of_parent_with_custom_init__should_not_use_head_parent():
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(StrippingChild).field("x").had(type=int))),
        Version("2000-01-01"),
    )

    old_child = generate_versioned_models(versions)["2000-01-01"][StrippingChild]

    assert StrippingChild(x=" a ").x == "a"
    # The __init__ of the head parent would fail to strip the integer
    assert old_child.model_validate({"x": 1}).model_dump() == {"x": 1}


@pytest.mark.parametrize(
    ("response_model", "latest_body"),
    [
        pytest.param(Animal, Dog(name="a", is_good=True), id="parent"),
        pytest.param(AnimalEnvelope, {"value": Dog(name="a", is_good=True)}, id="serialize_as_any"),
    ],
)
def test__migrate_response_body__head_instance_of_changed_subclass__should_be_rejected(
    response_model: type[BaseModel], latest_body: Any
):
    versions = VersionBundle(
        Version("2001-01-01", version_change(schema(Dog).field("is_good").didnt_exist)),
        Version("2000-01-01"),
    )

    # The head parent would accept the head instance and serialize it with the field that did not exist
    with pytest.raises(ValidationError):
        migrate_response_body(versions, response_model, latest_body=latest_body, version="2000-01-01")


def test__router_generation__unchanged_self_referencing_sibling_of_changed_model__should_work(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()
    body = {"name": "a", "children": [{"name": "b", "children": []}]}

    @router.post("/categories", response_model=SiblingCategory)
    async def create_category(category: SiblingCategory) -> SiblingCategory:
        return category

    @router.post("/siblings", response_model=ChangedSibling)
    async def create_sibling(sibling: ChangedSibling) -> ChangedSibling:
        return sibling

    app = create_versioned_app(
        version_change(schema(ChangedSibling).field("name").had(description="Old name")), router=router
    )
    unversioned_client = TestClient(app)

    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={app.router.api_version_parameter_name: version})
        assert client.post("/categories", json=body).json() == body
        assert client.post("/siblings", json={"name": "a"}).json() == {"name": "a"}
        assert unversioned_client.get(f"/openapi.json?version={version}").status_code == HTTP_200_OK
    for generator in generate_versioned_models(app.versions).values():
        # The common base is copied for its changed subclass but its other subclasses do not need copies
        assert generator[SharedBase] is not SharedBase
        assert generator[SiblingCategory] is SiblingCategory


def test__generate_versioned_models__self_referencing_generic_parametrized_while_deciding__should_stay_reused():
    versions = VersionBundle(
        Version("2001-01-01", unrelated_version_change()),
        Version("2000-01-01"),
    )
    generator = generate_versioned_models(versions)["2000-01-01"]
    answers_during_parametrization: list[Any] = []
    HOOKED_TREE_GENERATORS.append((generator, answers_during_parametrization))
    try:
        # Like when another thread parametrizes the generic, the parametrization is a subclass of HookedTree
        # before pydantic sets its fields and its generic args
        old_tree = generator[HookedTree[float]]
    finally:
        HOOKED_TREE_GENERATORS.clear()

    # The parametrizations do not make their origin versioned and the answer that did not know about the
    # parametrization yet is not kept either
    assert answers_during_parametrization
    assert all(answer is HookedTree for answer in answers_during_parametrization)
    assert generator[HookedTree] is HookedTree
    assert old_tree.model_validate({"value": 1, "children": [{"value": 2}]}).model_dump() == {
        "value": 1.0,
        "children": [{"value": 2.0, "children": []}],
    }
