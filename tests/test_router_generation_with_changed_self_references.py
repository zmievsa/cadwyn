"""Models in reference cycles that need versioning, such as a self-referencing model with instructions (#419).

The unchanged ones are reused as they are (see test_router_generation_with_self_references.py).
"""

import importlib.metadata
import re
import threading
import typing
from collections.abc import Callable
from typing import Annotated, Any, ClassVar, Generic, Literal, Optional, TypeVar, Union

import pytest
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, computed_field
from starlette.status import HTTP_200_OK, HTTP_422_UNPROCESSABLE_CONTENT

from cadwyn import (
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
from cadwyn.structure.schemas import schema
from tests.conftest import CreateVersionedApp, version_change

ValueT = TypeVar("ValueT")
PYDANTIC_VERSION = tuple(int(part) for part in re.findall(r"\d+", importlib.metadata.version("pydantic"))[:3])
REQUIRES_PYDANTIC_THAT_REBUILDS_METADATA_ONCE = pytest.mark.skipif(
    PYDANTIC_VERSION < (2, 11, 5), reason="Copying reference cycles requires pydantic 2.11.5+"
)
pytestmark = REQUIRES_PYDANTIC_THAT_REBUILDS_METADATA_ONCE


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


class Node(BaseModel, Generic[ValueT]):
    name: str
    value: ValueT
    parent: "Optional[IntNode]" = None


IntNode = Node[int]
Node.model_rebuild()
IntNode.model_rebuild()


class PagedCategory(BaseModel):
    name: str
    children: "Optional[CategoryPage]" = None


# Pydantic 2.11.0 cannot parametrize a generic with the model that it is creating
CategoryPage = Page[PagedCategory]
PagedCategory.model_rebuild()


class Leaf(BaseModel):
    name: str


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


class ItemTree(BaseModel):
    item: ChangedItem
    note: Optional[Note] = None
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
    versions = VersionBundle(Version("2001-01-01", version_change(short_name(Book))), Version("2000-01-01"))
    generator = generate_versioned_models(versions)["2000-01-01"]

    old_author, old_book = generator[Author], generator[Book]
    assert old_author is not Author
    assert old_book is not Book
    assert old_author.model_fields["books"].annotation == list[old_book]
    assert old_book.model_fields["author"].annotation == Optional[old_author]
    assert typing.get_type_hints(old_author) == {"name": str, "books": list[old_book]}

    old_entry = generate_versioned_models(
        VersionBundle(Version("2001-01-01", version_change(short_name(RegistryEntry))), Version("2000-01-01"))
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


@pytest.mark.parametrize("page_route_first", [False, True])
def test__router_generation__changed_generic_tree_with_head_generic_args__should_not_depend_on_route_order(
    page_route_first: bool, create_versioned_app: CreateVersionedApp
):
    router = VersionedAPIRouter()

    def add_tree_route():
        @router.post("/tree", response_model=TreeNode[int])
        async def create_tree(tree: TreeNode[int]) -> Any:
            return tree

    def add_page_route():
        @router.post("/page", response_model=Page[TreeNode[int]])
        async def create_page(page: Page[TreeNode[int]]) -> Any:
            return page

    if page_route_first:
        add_page_route()
        add_tree_route()
    else:
        add_tree_route()
        add_page_route()

    app = create_versioned_app(version_change(short_name(TreeNode)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    invalid_tree = {"name": "abc", "value": 1, "children": [{"name": "toolong", "value": 2, "children": []}]}
    assert old_client.post("/tree", json=invalid_tree).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    valid_tree = {"name": "abc", "value": 1, "children": [{"name": "abc", "value": 2, "children": []}]}
    assert old_client.post("/tree", json=valid_tree).json() == valid_tree
    assert old_client.post("/page", json={"items": [valid_tree]}).json() == {"items": [valid_tree]}
    # Page[TreeNode[int]] keeps the latest TreeNode[int] (see the next test) so the OpenAPI of the old version has
    # both of them under numbered names, but the tree route uses its own copy
    old_openapi = old_client.get("/openapi.json?version=2000-01-01").json()
    tree_body_ref = old_openapi["paths"]["/tree"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    old_tree_schema = old_openapi["components"]["schemas"][tree_body_ref["$ref"].rsplit("/", 1)[-1]]
    assert old_tree_schema["properties"]["name"]["maxLength"] == 3
    assert old_tree_schema["properties"]["children"]["items"] == tree_body_ref


def test__router_generation__generic_args_that_refer_to_changed_cycles__should_keep_head_args(
    create_versioned_app: CreateVersionedApp,
):
    """A known limitation: generic args that lead to changed reference cycles keep the latest models.

    This is what Cadwyn always did for the generic args that lead back to the generic itself.
    """
    router = VersionedAPIRouter()

    @router.post("/categories", response_model=PagedCategory)
    async def create_category(category: PagedCategory) -> Any:
        return category

    @router.post("/pages", response_model=Page[Category])
    async def create_page(page: Page[Category]) -> Any:
        return page

    app = create_versioned_app(version_change(short_name(PagedCategory), short_name(Category)), router=router)
    old_client = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})

    assert old_client.post("/categories", json={"name": "toolong"}).status_code == HTTP_422_UNPROCESSABLE_CONTENT
    nested_body = {"name": "abc", "children": {"items": [{"name": "toolong", "children": None}]}}
    assert old_client.post("/categories", json=nested_body).json() == nested_body
    page_body = {"items": [{"name": "toolong", "children": []}]}
    assert old_client.post("/pages", json=page_body).json() == page_body


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


def test__migrate_response_body__changed_self_referencing_model__should_be_validated_with_old_model():
    versions = VersionBundle(Version("2001-01-01", version_change(short_name(Category))), Version("2000-01-01"))

    body = {"name": "abc", "children": [{"name": "abc", "children": []}]}
    migrated = migrate_response_body(versions, Category, latest_body=body, version="2000-01-01")
    assert type(migrated) is generate_versioned_models(versions)["2000-01-01"][Category]
    assert type(migrated.children[0]) is type(migrated)
    with pytest.raises(ValueError, match="at most 3 characters"):
        migrate_response_body(
            versions, Category, latest_body={"name": "abc", "children": [{"name": "toolong"}]}, version="2000-01-01"
        )


def test__migrate_response_body__changed_self_referencing_model_with_deferred_build__should_be_built():
    versions = VersionBundle(Version("2001-01-01", version_change(short_name(DeferredCategory))), Version("2000-01-01"))

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
    versions = VersionBundle(Version("2001-01-01", version_change(short_name(ChangedItem))), Version("2000-01-01"))
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
        pytest.param(
            ComputedCategory,
            "computed field 'first_child' of .*ComputedCategory",
            id="computed_field",
        ),
        pytest.param(IncompleteCategory, "IncompleteCategory.* not defined yet", id="undefined_names"),
    ],
)
def test__generate_versioned_models__cycle_that_cannot_be_copied__should_raise(
    model: type[BaseModel], error_message: str
):
    versions = VersionBundle(Version("2001-01-01", version_change(short_name(model))), Version("2000-01-01"))

    with pytest.raises(SchemaGenerationError, match=error_message):
        generate_versioned_models(versions)


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
    versions = VersionBundle(Version("2001-01-01", version_change(short_name(ChangedItem))), Version("2000-01-01"))
    generator = generate_versioned_models(versions)["2000-01-01"]
    HOOK_GENERATORS.append(generator)
    try:
        with pytest.raises(CadwynError, match="HookedItemTree was requested from outside of its reference cycle"):
            generator[HookedItemTree]
    finally:
        HOOK_GENERATORS.clear()
