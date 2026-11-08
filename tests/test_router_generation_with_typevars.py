import json
import sys
import types
from enum import Enum
from typing import Any, Generic, TypeVar, Union

import pytest
from fastapi import Body
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import TypeAliasType
from typing_extensions import TypeVar as ExtensionsTypeVar

from cadwyn import RequestInfo, VersionedAPIRouter, convert_request_to_next_version_for, enum, schema
from cadwyn.exceptions import SchemaGenerationError
from tests.conftest import CreateRuntimeSchemas, CreateVersionedApp, version_change


@pytest.mark.parametrize("type_var", [TypeVar, ExtensionsTypeVar])
@pytest.mark.parametrize(
    "kind", ["bound", "quoted_bound", "union_bound", "pipe_bound", "constraints", "default", "quoted_default"]
)
def test__router_generation__unparametrized_generic__should_reject_latest_shape_in_old_version(
    type_var: Any, kind: str, create_versioned_app: CreateVersionedApp
):
    class LocalItem(BaseModel):
        name: str

    if kind in {"default", "quoted_default"} and type_var is TypeVar and sys.version_info < (3, 13):
        pytest.skip("typing.TypeVar defaults require Python 3.13")
    kwargs = {
        "bound": {"bound": LocalItem},
        "quoted_bound": {"bound": "LocalItem"},
        "union_bound": {"bound": Union[LocalItem, int]},
        "pipe_bound": {"bound": LocalItem | int},
        "quoted_default": {"default": "LocalItem"},
        "constraints": {},
        "default": {"default": LocalItem},
    }[kind]
    payload_t = type_var("payload_t", *((LocalItem, int) if kind == "constraints" else ()), **kwargs)

    class LocalPage(BaseModel, Generic[payload_t]):  # ty: ignore[invalid-argument-type] # Runtime TypeVar matrix.
        item: payload_t  # ty: ignore[invalid-type-form] # Runtime TypeVar matrix.

    router = VersionedAPIRouter()

    @router.post("/items")
    def create_items(payload: LocalPage) -> None:
        pass

    app = create_versioned_app(version_change(schema(LocalItem).field("name").had(type=int)), router=router)
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    latest = TestClient(app, headers={"x-api-version": "2001-01-01"})
    assert old.post("/items", json={"item": {"name": "latest"}}).status_code == 422
    assert latest.post("/items", json={"item": {"name": "latest"}}).status_code == 200


@pytest.mark.parametrize(
    "placement", ["field", "bound", "nested_arg", "default_alias", "mixed_alias", "mixed_alias_typevar", "partial"]
)
def test__router_generation__nested_unparametrized_generic__should_validate_and_document_old_model(
    placement: str, create_versioned_app: CreateVersionedApp
):
    class Item(BaseModel):
        name: str

    ItemT = ExtensionsTypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    ValueT = ExtensionsTypeVar("ValueT", bound=Page)
    DefaultT = ExtensionsTypeVar("DefaultT", default=Item)
    UnusedT = TypeVar("UnusedT")

    class Wrapper(BaseModel, Generic[ValueT, UnusedT]):
        page: ValueT

    class Nested(BaseModel):
        page: Page

    default_alias = TypeAliasType("default_alias", list[DefaultT], type_params=(DefaultT,))
    mixed_alias = TypeAliasType("mixed_alias", tuple[UnusedT, Page], type_params=(UnusedT,))
    payload_type = {
        "field": Nested,
        "bound": Wrapper,
        "nested_arg": Wrapper[Page[ItemT], int],
        "default_alias": default_alias,
        "mixed_alias": mixed_alias[int],
        "mixed_alias_typevar": mixed_alias[UnusedT],
        "partial": Wrapper[ValueT, int],
    }[placement]
    router = VersionedAPIRouter()

    @router.post("/items")
    def create_items(payload: payload_type = Body()) -> None:  # ty: ignore[invalid-type-form] # Runtime annotation matrix.
        pass

    @router.get("/items", response_model=payload_type)
    def get_items() -> Any:
        return bodies["2000-01-01"]

    bodies = {
        "field": {"page": {"item": {"old_name": "Ada"}}},
        "bound": {"page": {"item": {"old_name": "Ada"}}},
        "nested_arg": {"page": {"item": {"old_name": "Ada"}}},
        "default_alias": [{"old_name": "Ada"}],
        "mixed_alias": [1, {"item": {"old_name": "Ada"}}],
        "mixed_alias_typevar": [1, {"item": {"old_name": "Ada"}}],
        "partial": {"page": {"item": {"old_name": "Ada"}}},
    }
    old_body = bodies[placement]
    latest_body = json.loads(json.dumps(old_body).replace('"old_name"', '"name"'))
    bodies = {"2000-01-01": old_body, "2001-01-01": latest_body}

    @convert_request_to_next_version_for("/items", ["POST"])
    def rename(request: RequestInfo) -> None:
        request.body = latest_body

    app = create_versioned_app(
        version_change(schema(Item).field("name").had(name="old_name"), rename=rename), router=router
    )
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    latest = TestClient(app, headers={"x-api-version": "2001-01-01"})
    assert old.post("/items", json=old_body).status_code == 200
    assert old.post("/items", json=latest_body).status_code == 422
    assert latest.post("/items", json=latest_body).status_code == 200
    assert old.get("/items").json() == old_body
    old_openapi = old.get("/openapi.json?version=2000-01-01").json()
    item_schemas = [v for v in old_openapi["components"]["schemas"].values() if v.get("title") == "Item"]
    assert item_schemas
    assert all("old_name" in v["properties"] and "name" not in v["properties"] for v in item_schemas)


@pytest.mark.parametrize("kind", ["bound", "constraints", "default"])
def test__generate_versioned_models__enum_in_typevar__should_validate_old_members(
    kind: str, create_runtime_schemas: CreateRuntimeSchemas
):
    class Kind(str, Enum):
        old = "old"
        new = "new"

    factory: Any = sys.modules["typing_extensions"].TypeVar
    kwargs = {"bound": {"bound": Kind}, "constraints": {}, "default": {"default": Kind}}[kind]
    value_t = factory("ValueT", *((Kind, int) if kind == "constraints" else ()), **kwargs)

    class Payload(BaseModel, Generic[value_t]):  # ty: ignore[invalid-argument-type] # Runtime TypeVar matrix.
        value: value_t  # ty: ignore[invalid-type-form] # Runtime TypeVar matrix.

    schemas = create_runtime_schemas(version_change(enum(Kind).didnt_have("new")))
    old = schemas["2000-01-01"][Payload]
    assert old.model_validate({"value": "old"}).model_dump(mode="json") == {"value": "old"}
    with pytest.raises(ValidationError):
        old.model_validate({"value": "new"})
    assert schemas["2001-01-01"][Payload].model_validate({"value": "new"}).value == Kind.new
    assert Payload.model_validate({"value": "new"}).value is Kind.new


@pytest.mark.parametrize("syntax", ["bound", "quoted_bound", "constraints", "default"])
def test__generate_versioned_models__native_type_params__should_validate_old_model(
    syntax: str, create_runtime_schemas: CreateRuntimeSchemas, monkeypatch: pytest.MonkeyPatch
):
    if sys.version_info < (3, 12) or (syntax == "default" and sys.version_info < (3, 13)):
        pytest.skip("Native type parameters/defaults require Python 3.12/3.13")

    class Item(BaseModel):
        name: str

    declaration = {
        "bound": "T: Item",
        "quoted_bound": 'T: "Item"',
        "constraints": "T: (Item, int)",
        "default": "T = Item",
    }[syntax]
    module = types.ModuleType("_cadwyn_test_native_typevars")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    module.__dict__.update(BaseModel=BaseModel, Item=Item)
    exec(f"class Page[{declaration}](BaseModel):\n    item: T", module.__dict__)
    page = module.__dict__["Page"]
    # Native lazy bounds resolve their definition namespace, including locally defined Item.
    schemas = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))
    old = schemas["2000-01-01"][page]
    assert old.model_validate({"item": {"name": 1}}).item.name == 1
    with pytest.raises(ValidationError):
        old.model_validate({"item": {"name": "latest"}})


def test__generate_versioned_models__parametrizing_copied_generic__should_preserve_typevar_mapping(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = ExtensionsTypeVar("ItemT", bound=Item, default=Item, covariant=True)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    schemas = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))
    old = schemas["2000-01-01"][Page]
    int_page = old[int]
    default_page = old[()]
    assert isinstance(int_page, type)
    assert issubclass(int_page, BaseModel)
    assert isinstance(default_page, type)
    assert issubclass(default_page, BaseModel)
    assert int_page.model_validate({"item": 1}).model_dump() == {"item": 1}
    assert default_page.model_validate({"item": {"name": 1}}).model_dump() == {"item": {"name": 1}}
    assert Page.model_validate({"item": {"name": "latest"}}).item.name == "latest"
    assert ItemT.__bound__ is Item
    assert ItemT.__default__ is Item


def test__generate_versioned_models__unused_typevar_with_unresolved_bound__should_generate_models(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    UnusedT = TypeVar("UnusedT", bound="NeverDefined")  # ty: ignore[unresolved-reference] # noqa: F821 # Intentionally unresolved.

    class Page(BaseModel, Generic[UnusedT]):
        count: int

    schemas = create_runtime_schemas(version_change(schema(Page).field("count").had(description="Count")))
    assert schemas["2000-01-01"][Page].model_validate({"count": 1}).count == 1


def test__generate_versioned_models__recursive_typevar_bound__should_fail_each_startup_attempt(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    NodeT = TypeVar("NodeT", bound="Node")

    class Node(BaseModel, Generic[NodeT]):
        model_config = ConfigDict(defer_build=True)
        child: NodeT
        count: int = 0

    for _ in range(2):
        with pytest.raises(SchemaGenerationError, match=r"Node.*refer back to it"):
            create_runtime_schemas(version_change(schema(Node).field("count").had(description="Count")))["2000-01-01"][
                Node
            ]


def test__generate_versioned_models__unchanged_unresolved_bound__should_preserve_deferred_validation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    MissingT = TypeVar("MissingT", bound="NeverDefined")  # ty: ignore[unresolved-reference] # noqa: F821 # Intentionally unresolved.

    class Page(BaseModel, Generic[MissingT]):
        model_config = ConfigDict(defer_build=True)
        item: MissingT

    old = create_runtime_schemas()["2000-01-01"][Page]
    assert old.model_fields["item"].annotation is MissingT


@pytest.mark.parametrize("change_parent", [False, True])
def test__generate_versioned_models__typevar_bound_keeps_recursive_generic_args__should_fail_each_attempt(
    create_runtime_schemas: CreateRuntimeSchemas,
    change_parent: bool,
):
    ItemT = TypeVar("ItemT")

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    class Parent(BaseModel):
        name: str

    class Node(Parent):
        children: Union[Page["Node"], None] = None

    BoundT = TypeVar("BoundT", bound=Page[Node])

    class Payload(BaseModel, Generic[BoundT]):
        page: BoundT

    changed_model = Parent if change_parent else Node
    generator = create_runtime_schemas(version_change(schema(changed_model).field("name").had(type=int)))["2000-01-01"]
    for _ in range(2):
        with pytest.raises(SchemaGenerationError, match=r"type parameter BoundT.*latest.*Node"):
            generator[Payload]


def test__generate_versioned_models__recursive_builtin_alias_default__should_preserve_validation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    json_data = TypeAliasType("json_data", Union[str, list["json_data"]])
    JsonT = ExtensionsTypeVar("JsonT", default=json_data)

    class Envelope(BaseModel, Generic[JsonT]):
        item: JsonT
        count: int = 0

    schemas = create_runtime_schemas(version_change(schema(Envelope).field("count").had(description="Count")))
    assert schemas["2000-01-01"][Envelope].model_validate({"item": ["a", ["b"]]}).item == ["a", ["b"]]


@pytest.mark.parametrize("response_model", [None, int])
def test__router_generation__unvalidated_generic_return__should_preserve_explicit_response_model(
    response_model: Any,
    create_versioned_app: CreateVersionedApp,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    router = VersionedAPIRouter()

    @router.get("/items", response_model=response_model)
    def get_items() -> Page:
        return 1  # ty: ignore[invalid-return-type] # FastAPI deliberately ignores this return annotation.

    app = create_versioned_app(version_change(schema(Item).field("name").had(type=int)), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        response = TestClient(app, headers={"x-api-version": version}).get("/items")
        assert response.status_code == 200
        assert response.json() == 1


@pytest.mark.parametrize("kind", ["tuple_bound", "alias_default"])
def test__generate_versioned_models__composite_typevar__should_validate_old_nested_models(
    kind: str,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    alias = TypeAliasType("alias", tuple[Item, Item])
    factory: Any = sys.modules["typing_extensions"].TypeVar
    kwargs = {"bound": tuple[Item, Item]} if kind == "tuple_bound" else {"default": alias}
    value_t = factory("value_t", **kwargs)

    class Payload(BaseModel, Generic[value_t]):  # ty: ignore[invalid-argument-type] # Runtime TypeVar matrix.
        item: value_t  # ty: ignore[invalid-type-form] # Runtime TypeVar matrix.

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Payload]
    assert old.model_validate({"item": [{"name": 1}, {"name": 2}]}).model_dump(mode="json") == {
        "item": [{"name": 1}, {"name": 2}],
    }
    with pytest.raises(ValidationError):
        old.model_validate({"item": [{"name": "latest"}, {"name": "latest"}]})


@pytest.mark.parametrize(
    ("usage", "method", "old_status"),
    [("ignored_return", "GET", 200), ("replaced_return", "GET", 200), ("latest_only", "POST", 404)],
)
def test__router_generation__unsafe_bound_not_validated_in_old_versions__should_allow_startup(
    usage: str,
    method: str,
    old_status: int,
    create_versioned_app: CreateVersionedApp,
):
    from cadwyn import endpoint

    ItemT = TypeVar("ItemT")

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    class Node(BaseModel):
        name: str
        children: Union[Page["Node"], None] = None

    BoundT = TypeVar("BoundT", bound=Page[Node])

    class Payload(BaseModel, Generic[BoundT]):
        page: BoundT

    router = VersionedAPIRouter()
    instructions: list[Any] = [schema(Node).field("name").had(type=int)]
    if usage == "latest_only":

        @router.post("/items")
        def create_items(payload: Payload) -> None:
            pass

        instructions.append(endpoint("/items", ["POST"]).didnt_exist)
    else:

        @router.get("/items", response_model=None if usage == "ignored_return" else int)
        def get_items() -> Payload:
            return 1  # ty: ignore[invalid-return-type] # This return annotation is deliberately ignored.

    app = create_versioned_app(version_change(*instructions), router=router)
    client = TestClient(app, headers={"x-api-version": "2001-01-01"})
    body = {"page": {"item": {"name": "latest"}}}
    assert client.request(method, "/items", json=body).status_code == 200
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    assert old.request(method, "/items", json=body).status_code == old_status


def test__generate_versioned_models__unchanged_typevar_bound__should_accept_original_model_instances(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    old = create_runtime_schemas()["2000-01-01"][Page]
    item = Item(name="unchanged")
    assert old.model_validate({"item": item}).item is item


def test__generate_versioned_models__quoted_alias_default__should_resolve_alias_module(
    create_runtime_schemas: CreateRuntimeSchemas,
    monkeypatch: pytest.MonkeyPatch,
):
    module = types.ModuleType("_cadwyn_test_quoted_alias_default")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(
        "from pydantic import BaseModel\n"
        "from typing_extensions import TypeVar, TypeAliasType\n"
        "class Item(BaseModel):\n    name: str\n"
        "T = TypeVar('T', default='Item')\n"
        "Alias = TypeAliasType('Alias', list[T], type_params=(T,))\n"
        "class Envelope(BaseModel):\n    items: Alias\n",
        module.__dict__,
    )
    item = module.__dict__["Item"]
    envelope = module.__dict__["Envelope"]
    old = create_runtime_schemas(version_change(schema(item).field("name").had(type=int)))["2000-01-01"][envelope]
    assert old.model_validate({"items": [{"name": 1}]}).model_dump() == {"items": [{"name": 1}]}
    with pytest.raises(ValidationError):
        old.model_validate({"items": [{"name": "latest"}]})


def test__generate_versioned_models__parametrized_alias_default__should_version_bare_generic_in_alias(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)
    ArgT = TypeVar("ArgT")

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    alias = TypeAliasType("alias", tuple[ArgT, Page], type_params=(ArgT,))
    PayloadT = ExtensionsTypeVar("PayloadT", default=alias[int])

    class Envelope(BaseModel, Generic[PayloadT]):
        payload: PayloadT

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Envelope]
    assert old.model_validate({"payload": [1, {"item": {"name": 2}}]}).model_dump(mode="json") == {
        "payload": [1, {"item": {"name": 2}}],
    }
    with pytest.raises(ValidationError):
        old.model_validate({"payload": [1, {"item": {"name": "latest"}}]})
