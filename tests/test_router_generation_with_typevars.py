"""TypeVars that pydantic validates as their bounds, constraints, or defaults because nothing replaces them.

That is the parameters of generics used without type args (e.g. `Page` instead of `Page[Item]`), the parameters
that partially parametrized generics and type aliases leave, and the TypeVars in the fields of non-generic models.
"""

import json
import sys
import types
from enum import Enum
from typing import Annotated, Any, ClassVar, Generic, Optional, TypeVar, Union

import pytest
from fastapi import Body, Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import TypeAliasType, override
from typing_extensions import TypeVar as ExtensionsTypeVar

from cadwyn import (
    RequestInfo,
    ResponseInfo,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    endpoint,
    enum,
    schema,
)
from cadwyn.exceptions import SchemaGenerationError
from tests._data import typevar_namespace_first, typevar_namespace_second
from tests.conftest import CreateRuntimeSchemas, CreateVersionedApp, version_change

# Quoted references back to the model resolve in the module, like in its other fields
_SelfBoundT = TypeVar("_SelfBoundT", bound="SelfBoundNode")
_SelfDefaultT = ExtensionsTypeVar("_SelfDefaultT", default="SelfDefaultNode")
_NonGenericSelfBoundT = TypeVar("_NonGenericSelfBoundT", bound="NonGenericSelfBoundNode")


class SelfBoundNode(BaseModel, Generic[_SelfBoundT]):
    child: Union[_SelfBoundT, None] = None
    name: str


class SelfDefaultNode(BaseModel, Generic[_SelfDefaultT]):
    child: Union[_SelfDefaultT, None] = None
    name: str


class NonGenericSelfBoundNode(BaseModel):
    child: Union[_NonGenericSelfBoundT, None] = None  # ty: ignore[unbound-type-variable] # Validated as its bound.
    name: str


class RecursiveItem(BaseModel):
    name: str


_MixedSelfT = TypeVar("_MixedSelfT", "MixedSelfNode", RecursiveItem)


class MixedSelfNode(BaseModel, Generic[_MixedSelfT]):
    child: Union[_MixedSelfT, None] = None


_RecursiveItemT = TypeVar("_RecursiveItemT", bound=RecursiveItem)


class RecursiveNode(BaseModel, Generic[_RecursiveItemT]):
    item: _RecursiveItemT
    children: list["RecursiveNode[_RecursiveItemT]"] = []


_FirstT = TypeVar("_FirstT", bound="_SecondT")  # ty: ignore[invalid-type-variable-bound] # Deliberately recursive.
_SecondT = TypeVar("_SecondT", bound=_FirstT)  # ty: ignore[invalid-type-variable-bound] # Deliberately recursive.
_alias_of_recursive_fallbacks = TypeAliasType("_alias_of_recursive_fallbacks", list[_FirstT], type_params=(_FirstT,))


_ReentrantT = TypeVar("_ReentrantT", bound="ReentrantPage")
_ReentrantInnerT = TypeVar("_ReentrantInnerT")


class ReentrantInner(BaseModel, Generic[_ReentrantInnerT]):
    value: _ReentrantInnerT


class ReentrantPage(BaseModel, Generic[_ReentrantT]):
    inner: ReentrantInner[_ReentrantT]
    name: str


_SelfReferringT = TypeVar("_SelfReferringT", bound="MiddleSelfReferringNode")


class MiddleSelfReferringNode(BaseModel, Generic[_SelfReferringT]):
    child: Optional[_SelfReferringT] = None
    name: str


# Sorted by name, one of them comes before the node and the other after it. Their fields use the node first and last
class AOuterWithNodeFirst(BaseModel, Generic[_SelfReferringT]):
    node: MiddleSelfReferringNode[_SelfReferringT]
    item: _SelfReferringT


class ZOuterWithItemFirst(BaseModel, Generic[_SelfReferringT]):
    item: _SelfReferringT
    node: MiddleSelfReferringNode[_SelfReferringT]


_FactoryBoundT = TypeVar("_FactoryBoundT", bound="FirstFactoryModel")  # ty: ignore[invalid-type-form] # A class.


def _make_factory_model() -> Any:
    class Model(BaseModel, Generic[_FactoryBoundT]):
        model_config = ConfigDict(defer_build=True)
        child: Optional[_FactoryBoundT] = None
        name: str

    return Model


# Two models with the same module and qualname
FirstFactoryModel = _make_factory_model()
SecondFactoryModel = _make_factory_model()
FirstFactoryModel.model_rebuild()
SecondFactoryModel.model_rebuild()


class OuterOfFactoryModels(BaseModel, Generic[_FactoryBoundT]):
    first: FirstFactoryModel[_FactoryBoundT]  # ty: ignore[invalid-type-form] # A class from a factory.
    second: SecondFactoryModel[_FactoryBoundT]  # ty: ignore[invalid-type-form] # A class from a factory.


# The bound and the constraints name both models that use the parameter
_BoundToBothT = TypeVar("_BoundToBothT", bound=Union["BoundFirst", "BoundSecond"])
_ConstrainedToBothT = TypeVar("_ConstrainedToBothT", "ConstrainedFirst", "ConstrainedSecond")


class BoundFirst(BaseModel, Generic[_BoundToBothT]):
    model_config = ConfigDict(defer_build=True)
    name: str
    child: Optional[_BoundToBothT] = None


class BoundSecond(BaseModel, Generic[_BoundToBothT]):
    model_config = ConfigDict(defer_build=True)
    name: str
    child: Optional[_BoundToBothT] = None


class BoundOuterWithFirstFirst(BaseModel, Generic[_BoundToBothT]):
    first: BoundFirst[_BoundToBothT]
    second: BoundSecond[_BoundToBothT]


class BoundOuterWithSecondFirst(BaseModel, Generic[_BoundToBothT]):
    second: BoundSecond[_BoundToBothT]
    first: BoundFirst[_BoundToBothT]


class ConstrainedFirst(BaseModel, Generic[_ConstrainedToBothT]):
    model_config = ConfigDict(defer_build=True)
    name: str
    child: Optional[_ConstrainedToBothT] = None


class ConstrainedSecond(BaseModel, Generic[_ConstrainedToBothT]):
    model_config = ConfigDict(defer_build=True)
    name: str
    child: Optional[_ConstrainedToBothT] = None


class ConstrainedOuterWithFirstFirst(BaseModel, Generic[_ConstrainedToBothT]):
    first: ConstrainedFirst[_ConstrainedToBothT]
    second: ConstrainedSecond[_ConstrainedToBothT]


class ConstrainedOuterWithSecondFirst(BaseModel, Generic[_ConstrainedToBothT]):
    second: ConstrainedSecond[_ConstrainedToBothT]
    first: ConstrainedFirst[_ConstrainedToBothT]


for _model in (BoundFirst, BoundSecond, ConstrainedFirst, ConstrainedSecond):
    _model.model_rebuild()


class ShapeItem(BaseModel):
    name: str


_ShapeT = TypeVar("_ShapeT", bound=ShapeItem)


class ShapePage(BaseModel, Generic[_ShapeT]):
    item: _ShapeT


class ShapeItemPage(ShapePage):
    pass


class ShapeFirst(BaseModel, Generic[_ShapeT]):
    item: _ShapeT
    second: Optional["ShapeSecond[_ShapeT]"] = None
    other_second: Optional["ShapeSecond[_ShapeT]"] = None


class ShapeSecond(BaseModel, Generic[_ShapeT]):
    item: _ShapeT
    first: Optional[ShapeFirst[_ShapeT]] = None


ShapeFirst.model_rebuild()


class ShapeWithClassVar(BaseModel, Generic[_ShapeT]):
    marker: ClassVar[type[ShapeItem]] = ShapeItem
    item: _ShapeT


class _TupleWithSchemaHook(tuple):
    """Metadata that pydantic uses and that Cadwyn copies like other tuples"""

    __slots__ = ()

    def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> Any:
        return handler(source)


class ShapeWithItselfInUnusedMetadata(BaseModel, Generic[_ShapeT]):
    item: _ShapeT
    marker: "Annotated[int, [ShapeWithItselfInUnusedMetadata]]" = 0


class AliasDefaultItem(BaseModel):
    name: str


_QuotedDefaultT = ExtensionsTypeVar("_QuotedDefaultT", default="AliasDefaultItem")
_alias_with_quoted_default = TypeAliasType(
    "_alias_with_quoted_default", list[_QuotedDefaultT], type_params=(_QuotedDefaultT,)
)


class AliasDefaultEnvelope(BaseModel):
    items: _alias_with_quoted_default


@pytest.mark.parametrize("type_var", [TypeVar, ExtensionsTypeVar])
@pytest.mark.parametrize("kind", ["bound", "quoted_bound", "union_bound", "constraints", "default"])
def test__router_generation__unparametrized_generic__should_reject_latest_shape_in_old_version(
    type_var: Any, kind: str, create_versioned_app: CreateVersionedApp
):
    class LocalItem(BaseModel):
        name: str

    if kind == "default" and type_var is TypeVar and sys.version_info < (3, 13):
        pytest.skip("typing.TypeVar defaults require Python 3.13")
    kwargs = {
        "bound": {"bound": LocalItem},
        "quoted_bound": {"bound": "LocalItem"},
        "union_bound": {"bound": Union[LocalItem, int]},
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


def test__router_generation__unparametrized_generic__should_keep_openapi_names_and_titles(
    create_versioned_app: CreateVersionedApp,
):
    class Item(BaseModel):
        name: str
        extra: int = 0

    ItemT = TypeVar("ItemT", bound=Item)
    OtherT = TypeVar("OtherT")

    class Page(BaseModel, Generic[ItemT]):
        """A page of items."""

        items: list[ItemT]

    class Pair(BaseModel, Generic[OtherT, ItemT]):
        other: OtherT
        item: ItemT

    router = VersionedAPIRouter()

    @router.post("/page", response_model=Page)
    def exchange_page(page: Page) -> Any:
        return page

    @router.post("/pair")
    def create_pair(pair: Pair[int, ItemT]) -> None:
        pass

    # Item only changes in the oldest version, so nothing changes between the two newer ones
    app = create_versioned_app(version_change(schema(Item).field("extra").didnt_exist), version_change(), router=router)
    titles_by_name = None
    for version in ("2002-01-01", "2001-01-01", "2000-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        schemas = client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        version_titles_by_name = {name: value["title"] for name, value in schemas.items()}
        # Pydantic names Pair[int, ItemT] after the repr of ItemT, which differs between Python versions
        assert {"Item": "Item", "Page": "Page"}.items() < version_titles_by_name.items()
        assert any(name.startswith("Pair_int_") for name in version_titles_by_name)
        assert version_titles_by_name == (titles_by_name or version_titles_by_name)
        titles_by_name = version_titles_by_name
        assert schemas["Page"]["description"] == "A page of items."
        assert schemas["Page"]["properties"]["items"]["items"] == {"$ref": "#/components/schemas/Item"}
        assert ("extra" in schemas["Item"]["properties"]) is (version != "2000-01-01")
        expected_item = {"name": "a"} if version == "2000-01-01" else {"name": "a", "extra": 0}
        assert client.post("/page", json={"items": [{"name": "a"}]}).json() == {"items": [expected_item]}
        assert client.post("/pair", json={"other": 1, "item": {"name": "a"}}).status_code == 200


@pytest.mark.parametrize(
    ("mode", "expected_titles_by_name"),
    [
        # Cadwyn 8.2.0 gives every version these names and titles
        pytest.param("unchanged", {"Item": "Item", "Outer": "Outer", "Page": "Page"}, id="unchanged_quoted_bound"),
        pytest.param("changed", {"Item": "Item", "Outer": "Outer", "Page": "Page"}, id="nested"),
        pytest.param("recursive", {"Item": "Item", "Node": "Node"}, id="recursive"),
    ],
)
def test__router_generation__nested_and_recursive_unparametrized_generics__should_keep_openapi_names(
    mode: str, expected_titles_by_name: dict[str, str], create_versioned_app: CreateVersionedApp
):
    class Item(BaseModel):
        name: str

    class Unrelated(BaseModel):
        count: int

    # A quoted bound needs resolving even when nothing about it changes
    bound: Any = "Item" if mode == "unchanged" else Item
    ItemT = TypeVar("ItemT", bound=bound)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    class Outer(BaseModel, Generic[ItemT]):
        page: Page[ItemT]

    class Node(BaseModel, Generic[ItemT]):
        item: ItemT
        children: list["Node[ItemT]"] = []

    model = Node if mode == "recursive" else Outer
    router = VersionedAPIRouter()

    def exchange(body: Any) -> Any: ...

    exchange.__annotations__["body"] = model
    router.post("/items", response_model=model)(exchange)
    instruction = (
        schema(Unrelated).field("count").had(description="Old")
        if mode == "unchanged"
        else schema(Item).field("name").had(type=int)
    )
    app = create_versioned_app(version_change(instruction), router=router)
    for version in ("2001-01-01", "2000-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        schemas = client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        # Whether pydantic splits a schema into "-Input" and "-Output" ones depends on the versions of FastAPI
        # and pydantic, just like with Cadwyn 8.2.0
        titles_by_name = {
            name.removesuffix("-Input").removesuffix("-Output"): value["title"]
            for name, value in schemas.items()
            if "ValidationError" not in name
        }
        assert titles_by_name == expected_titles_by_name


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


@pytest.mark.parametrize("usage", ["body", "generic_arg", "dependency"])
def test__router_generation__typevar_in_non_generic_model__should_validate_and_document_old_model(
    usage: str, create_versioned_app: CreateVersionedApp
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)
    ArgT = TypeVar("ArgT")

    class Plain(BaseModel):
        item: ItemT  # ty: ignore[unbound-type-variable] # Pydantic validates the bound of an unbound TypeVar.

    class Box(BaseModel, Generic[ArgT]):
        value: ArgT

    router = VersionedAPIRouter()
    if usage == "dependency":

        def get_name(plain: Plain) -> str:
            return "name"

        @router.post("/items")
        def create_items(name: Annotated[str, Depends(get_name)]) -> None:
            pass

    else:
        payload_type = Box[Plain] if usage == "generic_arg" else Plain

        @router.post("/items")
        def create_items(payload: payload_type) -> None:  # ty: ignore[invalid-type-form] # Runtime annotation matrix.
            pass

    def wrap(item: dict[str, Any]) -> dict[str, Any]:
        return {"value": {"item": item}} if usage == "generic_arg" else {"item": item}

    @convert_request_to_next_version_for("/items", ["POST"])
    def rename(request: RequestInfo) -> None:
        request.body = wrap({"name": "latest"})

    app = create_versioned_app(
        version_change(schema(Item).field("name").had(name="old_name"), rename=rename), router=router
    )
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    assert old.post("/items", json=wrap({"old_name": "a"})).status_code == 200
    assert old.post("/items", json=wrap({"name": "a"})).status_code == 422
    old_openapi = old.get("/openapi.json?version=2000-01-01").json()
    assert old_openapi["components"]["schemas"]["Item"]["properties"].keys() == {"old_name"}


@pytest.mark.parametrize(
    ("model", "body"),
    [
        pytest.param(ShapeItemPage, {"item": {"name": 1}}, id="subclass"),
        pytest.param(
            ShapeFirst,
            {"item": {"name": 1}, "second": {"item": {"name": 2}, "first": {"item": {"name": 3}}}},
            id="mutual_recursion",
        ),
        pytest.param(ShapeWithClassVar, {"item": {"name": 1}}, id="class_var"),
    ],
)
def test__generate_versioned_models__unparametrized_generic_shapes__should_validate_old_model(
    model: type[BaseModel], body: dict[str, Any], create_runtime_schemas: CreateRuntimeSchemas
):
    old = create_runtime_schemas(version_change(schema(ShapeItem).field("name").had(type=int)))["2000-01-01"][model]
    assert old.__name__ == model.__name__
    assert old.model_validate(body).model_dump(exclude_unset=True) == body
    with pytest.raises(ValidationError):
        old.model_validate(json.loads(json.dumps(body).replace("3}", '"latest"}').replace("1}", '"latest"}')))


def test__router_generation__unparametrized_generic_with_itself_in_unused_metadata__should_start_and_validate(
    create_versioned_app: CreateVersionedApp,
):
    router = VersionedAPIRouter()

    @router.post("/items", response_model=ShapeWithItselfInUnusedMetadata)
    def exchange(body: ShapeWithItselfInUnusedMetadata) -> Any:
        return body

    @convert_request_to_next_version_for("/items", ["POST"])
    def migrate(request: RequestInfo) -> None:
        request.body["item"]["name"] = str(request.body["item"]["name"])

    @convert_response_to_previous_version_for("/items", ["POST"])
    def migrate_back(response: ResponseInfo) -> None:
        response.body["item"]["name"] = int(response.body["item"]["name"])

    # Pydantic ignores the metadata, as does Cadwyn 8.2.0
    app = create_versioned_app(
        version_change(schema(ShapeItem).field("name").had(type=int), migrate=migrate, migrate_back=migrate_back),
        router=router,
    )
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    assert old.post("/items", json={"item": {"name": 1}, "marker": "2"}).json() == {"item": {"name": 1}, "marker": 2}
    assert old.post("/items", json={"item": {"name": "latest"}}).status_code == 422


def test__generate_versioned_models__unparametrized_generic_with_itself_in_used_metadata__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Page(BaseModel, Generic[_ShapeT]):
        item: _ShapeT
        marker: "Annotated[int, _TupleWithSchemaHook((Page,))]" = 0

    assert Page.model_validate({"item": {"name": "a"}, "marker": "1"}).marker == 1

    schemas = create_runtime_schemas(version_change(schema(ShapeItem).field("name").had(type=int)))
    with pytest.raises(SchemaGenerationError, match=r"Page.*refers to itself in metadata that pydantic uses"):
        schemas["2000-01-01"][Page]


@pytest.mark.parametrize("parametrized", [False, True])
def test__generate_versioned_models__typed_extras_of_nested_generic__should_validate_old_model(
    parametrized: bool, create_runtime_schemas: CreateRuntimeSchemas
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)
    OtherT = TypeVar("OtherT")

    class Extra(BaseModel, Generic[ItemT]):
        model_config = ConfigDict(extra="allow")
        __pydantic_extra__: dict[str, ItemT]  # ty: ignore[incompatible-mutable-override] # Pydantic supports typed extras.

    class Outer(BaseModel, Generic[OtherT, ItemT]):
        other: OtherT
        extras: Extra[ItemT]

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"]
    if parametrized:
        # Pydantic versions differ in how they substitute parameters in the typed extras of parametrizations
        with pytest.raises(SchemaGenerationError, match=r"typed extra fields.*Outer.*ItemT.*2000-01-01"):
            old[Outer[int, ItemT]]
    else:
        old_outer = old[Outer]
        body = {"other": 1, "extras": {"x": {"name": 1}}}
        assert old_outer.model_validate(body).model_dump() == body
        with pytest.raises(ValidationError):
            old_outer.model_validate({"other": 1, "extras": {"x": {"name": "latest"}}})
        item_schema = old_outer.model_json_schema()["$defs"]["Item"]
        assert item_schema["properties"]["name"]["type"] == "integer"


@pytest.mark.parametrize("node", [SelfBoundNode, SelfDefaultNode, NonGenericSelfBoundNode])
def test__router_generation__self_bound_typevar__should_preserve_recursive_requests_and_openapi(
    node: type[BaseModel], create_versioned_app: CreateVersionedApp
):
    router = VersionedAPIRouter()

    def exchange(body: Any) -> Any:
        return body

    exchange.__annotations__["body"] = node
    router.post("/items", response_model=node)(exchange)
    app = create_versioned_app(version_change(schema(node).field("name").had(description="old")), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/items", json={"name": "a", "child": {"name": "b"}})
        assert response.status_code == 200
        assert response.json() == {"child": {"child": None, "name": "b"}, "name": "a"}
        schemas = client.get(f"/openapi.json?version={version}").json()["components"]["schemas"]
        nodes = [value for value in schemas.values() if value["title"] == node.__name__]
        assert nodes
        expected_description = "old" if version == "2000-01-01" else None
        assert all(value["properties"]["name"].get("description") == expected_description for value in nodes)


def test__generate_versioned_models__fallback_referring_to_its_model_and_changed_model__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(RecursiveItem).field("name").had(type=int)))
    assert schemas["2001-01-01"][MixedSelfNode].model_validate({"child": {"name": "a"}}).model_dump() == {
        "child": {"name": "a"}
    }
    with pytest.raises(SchemaGenerationError, match=r"_MixedSelfT.*refer back to MixedSelfNode.*2000-01-01"):
        schemas["2000-01-01"][MixedSelfNode]


@pytest.mark.parametrize("parametrized", [False, True])
def test__router_generation__recursive_generic_with_bound__should_validate_each_version(
    parametrized: bool, create_versioned_app: CreateVersionedApp
):
    node = RecursiveNode[RecursiveItem] if parametrized else RecursiveNode
    router = VersionedAPIRouter()

    def exchange(payload: Any) -> Any:
        return payload

    exchange.__annotations__["payload"] = node
    router.post("/node", response_model=node)(exchange)
    app = create_versioned_app(
        version_change(schema(RecursiveItem).field("name").had(description="Old")), router=router
    )
    body = {"item": {"name": "A"}, "children": [{"item": {"name": "B"}}]}
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/node", json=body)
        assert response.status_code == 200
        assert response.json() == {"item": {"name": "A"}, "children": [{"item": {"name": "B"}, "children": []}]}
        assert client.get(f"/openapi.json?version={version}").status_code == 200


@pytest.mark.parametrize("kind", ["bound", "constraints", "default"])
def test__generate_versioned_models__enum_in_typevar__should_validate_old_members(
    kind: str, create_runtime_schemas: CreateRuntimeSchemas
):
    class Kind(str, Enum):
        old = "old"
        new = "new"

    kwargs = {"bound": {"bound": Kind}, "constraints": {}, "default": {"default": Kind}}[kind]
    # Runtime TypeVar matrix that static type checkers cannot follow
    factory: Any = ExtensionsTypeVar
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
    # The syntax of native type params fails to parse on Python 3.11
    exec(f"class Page[{declaration}](BaseModel):\n    item: T", module.__dict__)
    page = module.__dict__["Page"]
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


def test__generate_versioned_models__recursive_typevar_bound__should_preserve_deferred_validation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    NodeT = TypeVar("NodeT", bound="Node")

    class Node(BaseModel, Generic[NodeT]):
        model_config = ConfigDict(defer_build=True)
        child: Union[NodeT, None] = None
        count: int = 0

    for _ in range(2):
        old = create_runtime_schemas(version_change(schema(Node).field("count").had(description="Count")))[
            "2000-01-01"
        ][Node]
        assert old.model_validate({"child": {"count": 1}}).model_dump() == {
            "child": {"child": None, "count": 1},
            "count": 0,
        }


def test__generate_versioned_models__unchanged_unresolved_bound__should_preserve_deferred_validation(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    MissingT = TypeVar("MissingT", bound="NeverDefined")  # ty: ignore[unresolved-reference] # noqa: F821 # Intentionally unresolved.

    class Page(BaseModel, Generic[MissingT]):
        model_config = ConfigDict(defer_build=True)
        item: MissingT

    old = create_runtime_schemas()["2000-01-01"][Page]
    assert old.model_fields["item"].annotation is MissingT


def test__generate_versioned_models__unresolved_bare_parameter__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    MissingT = TypeVar("MissingT", bound="Missing")  # ty: ignore[unresolved-reference] # Deliberately unresolved. # noqa: F821

    class Page(BaseModel, Generic[MissingT]):
        model_config = ConfigDict(defer_build=True)
        item: MissingT
        count: int = 0

    class Wrapper(BaseModel):
        model_config = ConfigDict(defer_build=True)
        page: Page

    with pytest.raises(SchemaGenerationError, match=r"cannot resolve.*Missing.*MissingT"):
        create_runtime_schemas(version_change(schema(Page).field("count").had(description="Old")))["2000-01-01"][
            Wrapper
        ]


def test__generate_versioned_models__recursive_parameter_fallbacks__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Envelope(BaseModel):
        item: int

    with pytest.raises(SchemaGenerationError, match=r"type parameter _FirstT.*refer back to it"):
        create_runtime_schemas(version_change(schema(Envelope).field("item").had(type=_alias_of_recursive_fallbacks)))


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


def test__generate_versioned_models__bare_generic_overriding_parametrization__should_not_parametrize_it(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

        @override
        def __class_getitem__(cls, typevar_values: Any) -> Any:
            raise AssertionError("Cadwyn parametrized the generic")

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Page]
    assert old.model_validate({"item": {"name": 1}}).model_dump() == {"item": {"name": 1}}
    with pytest.raises(ValidationError):
        old.model_validate({"item": {"name": "latest"}})


def test__generate_versioned_models__partially_parametrized_generic_overriding_parametrization__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)
    OtherT = TypeVar("OtherT")
    is_overridden = False

    class Pair(BaseModel, Generic[OtherT, ItemT]):
        other: OtherT
        item: ItemT

        @override
        def __class_getitem__(cls, typevar_values: Any) -> Any:
            return int if is_overridden else super().__class_getitem__(typevar_values)

    partial_pair = Pair[int, ItemT]
    is_overridden = True
    schemas = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))
    with pytest.raises(SchemaGenerationError, match="cannot safely parametrize the bare generic"):
        schemas["2000-01-01"][partial_pair]


def test__generate_versioned_models__bare_generic_whose_nested_fallback_refers_to_it__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(ReentrantPage).field("name").had(description="Old")))
    with pytest.raises(SchemaGenerationError, match=r"ReentrantPage.*fallbacks refer back to it"):
        schemas["2000-01-01"][ReentrantPage]


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


@pytest.mark.parametrize("kind", ["tuple_bound", "alias_default"])
def test__generate_versioned_models__composite_typevar__should_validate_old_nested_models(
    kind: str,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    alias = TypeAliasType("alias", tuple[Item, Item])
    kwargs = {"bound": tuple[Item, Item]} if kind == "tuple_bound" else {"default": alias}
    factory: Any = ExtensionsTypeVar
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
            return 1  # ty: ignore[invalid-explicit-return] # This return annotation is deliberately ignored.

    app = create_versioned_app(version_change(*instructions), router=router)
    client = TestClient(app, headers={"x-api-version": "2001-01-01"})
    body = {"page": {"item": {"name": "latest"}}}
    assert client.request(method, "/items", json=body).status_code == 200
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    assert old.request(method, "/items", json=body).status_code == old_status


@pytest.mark.parametrize("nesting", ["direct", "nested"])
def test__router_generation__fully_parametrized_generic_with_recursive_bound__should_ignore_bound(
    nesting: str, create_versioned_app: CreateVersionedApp
):
    ItemT = TypeVar("ItemT")

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    class Node(BaseModel):
        name: str
        children: Union[Page["Node"], None] = None

    BoundT = TypeVar("BoundT", bound=Union[Page[Node], int])

    class Inner(BaseModel, Generic[BoundT]):
        value: BoundT

    class Outer(BaseModel, Generic[BoundT]):
        inner: Inner[BoundT]

    payload_type = Inner[int] if nesting == "direct" else Outer[int]
    body = {"value": 1} if nesting == "direct" else {"inner": {"value": 1}}
    router = VersionedAPIRouter()

    def exchange(payload: Any) -> Any:
        return payload

    exchange.__annotations__["payload"] = payload_type
    router.post("/items", response_model=payload_type)(exchange)
    app = create_versioned_app(
        version_change(schema(Node).field("name").had(type=int), schema(Inner).field("value").had(description="Old")),
        router=router,
    )
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/items", json=body)
        assert response.status_code == 200
        assert response.json() == body
        spec = client.get(f"/openapi.json?version={version}").json()
        assert not any(value.get("title") == "Node" for value in spec["components"]["schemas"].values())


@pytest.mark.parametrize("usage", ["classvar", "factory_annotation"])
def test__router_generation__bare_generic_in_unvalidated_metadata__should_ignore_fallback(
    usage: str,
    create_versioned_app: CreateVersionedApp,
):
    ItemT = TypeVar("ItemT")

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    class Node(BaseModel):
        name: str
        children: Union[Page["Node"], None] = None

    BoundT = TypeVar("BoundT", bound=Union[Page[Node], int])

    class Payload(BaseModel, Generic[BoundT]):
        page: BoundT

    def factory() -> Annotated[int, Payload]:
        return 1

    if usage == "classvar":

        class Wrapper(BaseModel):
            marker: ClassVar[type[Payload]] = Payload
            count: int = 1
    else:

        class Wrapper(BaseModel):
            count: int = Field(default_factory=factory)

    router = VersionedAPIRouter()

    @router.post("/items")
    def exchange(payload: Wrapper) -> int:
        return payload.count

    app = create_versioned_app(version_change(schema(Node).field("name").had(type=int)), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/items", json={})
        assert response.status_code == 200
        assert response.json() == 1


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


def test__router_generation__nested_bare_generic__should_keep_inner_instructions(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Inner(BaseModel, Generic[ItemT]):
        value: ItemT
        count: int

    class Outer(BaseModel, Generic[ItemT]):
        inner: Inner[ItemT]

    schemas = create_runtime_schemas(
        version_change(schema(Item).field("name").had(type=int), schema(Inner).field("count").had(name="old_count"))
    )
    old = schemas["2000-01-01"][Outer]
    assert old.model_validate({"inner": {"value": {"name": 1}, "old_count": 2}}).model_dump() == {
        "inner": {"value": {"name": 1}, "old_count": 2}
    }


def test__generate_versioned_models__bare_generic_using_alias_field__should_validate_old_bound(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)
    items = TypeAliasType("items", list[ItemT], type_params=(ItemT,))

    class Page(BaseModel, Generic[ItemT]):
        values: items[ItemT]

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Page]
    assert old.model_validate({"values": [{"name": 1}]}).model_dump() == {"values": [{"name": 1}]}
    with pytest.raises(ValidationError):
        old.model_validate({"values": [{"name": "latest"}]})


def test__generate_versioned_models__quoted_alias_default__should_resolve_alias_module(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    old = create_runtime_schemas(version_change(schema(AliasDefaultItem).field("name").had(type=int)))["2000-01-01"][
        AliasDefaultEnvelope
    ]
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


def test__generate_versioned_models__alias_with_unchanged_bare_generic__should_version_explicit_args(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=int)
    ArgT = TypeVar("ArgT")

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    alias = TypeAliasType("alias", tuple[ArgT, Page], type_params=(ArgT,))

    class Envelope(BaseModel):
        value: alias[Item]

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Envelope]
    assert old.model_validate({"value": [{"name": 1}, {"item": 2}]}).model_dump(mode="json") == {
        "value": [{"name": 1}, {"item": 2}]
    }


def test__router_generation__parametrized_alias_without_fallbacks__should_keep_latest_openapi(
    create_versioned_app: CreateVersionedApp,
):
    class Item(BaseModel):
        name: str

    ArgT = TypeVar("ArgT")
    pair = TypeAliasType("pair", tuple[ArgT, Item], type_params=(ArgT,))
    router = VersionedAPIRouter()

    @router.post("/items", response_model=pair[int])
    def exchange(payload: pair[int] = Body()) -> Any:
        return payload

    app = create_versioned_app(version_change(schema(Item).field("name").had(description="Old")), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        assert client.post("/items", json=[1, {"name": "A"}]).json() == [1, {"name": "A"}]
        spec = client.get(f"/openapi.json?version={version}").json()
        item = next(value for value in spec["components"]["schemas"].values() if value.get("title") == "Item")
        assert "description" not in item["properties"]["name"]


@pytest.mark.parametrize("changed_item", [None, typevar_namespace_first.Item])
def test__router_generation__shared_typevar_in_different_modules__should_resolve_each_namespace(
    changed_item: Union[type[BaseModel], None],
    create_versioned_app: CreateVersionedApp,
):
    class Payload(BaseModel):
        a: typevar_namespace_first.Page
        b: typevar_namespace_second.Page

    router = VersionedAPIRouter()

    @router.post("/items", response_model=Payload)
    def exchange(payload: Payload) -> Any:
        return payload

    change = (
        version_change(schema(changed_item).field("a").had(description="Old")) if changed_item else version_change()
    )
    app = create_versioned_app(change, router=router)
    body = {"a": {"item": {"a": "A"}}, "b": {"item": {"b": "B"}}}
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/items", json=body)
        assert response.status_code == 200
        assert response.json() == body
        spec = client.get(f"/openapi.json?version={version}").json()
        item_shapes = [
            set(value["properties"]) for value in spec["components"]["schemas"].values() if value.get("title") == "Item"
        ]
        assert {"a"} in item_shapes
        assert {"b"} in item_shapes


@pytest.mark.parametrize(
    ("envelope", "changed_item"),
    [
        (typevar_namespace_first.Envelope, typevar_namespace_second.Item),
        (typevar_namespace_first.EnvelopeWithItem, None),
    ],
)
def test__generate_versioned_models__bare_generic_sharing_quoted_parameter_across_namespaces__should_resolve_each(
    envelope: type[BaseModel],
    changed_item: Union[type[BaseModel], None],
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Unrelated(BaseModel):
        b: str

    changed = changed_item or Unrelated
    old = create_runtime_schemas(version_change(schema(changed).field("b").had(type=int)))["2000-01-01"][envelope]
    body: dict[str, Any] = {"page": {"item": {"b": 1 if changed_item else "B"}}}
    if envelope is typevar_namespace_first.EnvelopeWithItem:
        body["item"] = {"a": "A"}
    assert old.model_validate(body).model_dump() == body


@pytest.mark.parametrize(
    ("outer", "first", "second"),
    [
        pytest.param(BoundOuterWithFirstFirst, BoundFirst, BoundSecond, id="bound"),
        pytest.param(BoundOuterWithSecondFirst, BoundFirst, BoundSecond, id="bound_reversed"),
        pytest.param(ConstrainedOuterWithFirstFirst, ConstrainedFirst, ConstrainedSecond, id="constraints"),
        pytest.param(ConstrainedOuterWithSecondFirst, ConstrainedFirst, ConstrainedSecond, id="constraints_reversed"),
    ],
)
def test__generate_versioned_models__fallback_naming_several_models_that_use_it__should_fail_loudly(
    outer: Any, first: Any, second: Any, create_runtime_schemas: CreateRuntimeSchemas
):
    # Within the copy of each model, pydantic would resolve the name of the other one to its latest version
    schemas = create_runtime_schemas(
        version_change(schema(first).field("name").had(type=int), schema(second).field("name").had(type=int))
    )
    with pytest.raises(SchemaGenerationError, match=r"refer back to .*and also to models that change"):
        schemas["2000-01-01"][outer]


@pytest.mark.parametrize("outer", [AOuterWithNodeFirst, ZOuterWithItemFirst])
def test__generate_versioned_models__bare_generic_whose_nested_model_is_its_parameters_bound__should_validate_old_model(
    outer: type[BaseModel], create_runtime_schemas: CreateRuntimeSchemas
):
    schemas = create_runtime_schemas(version_change(schema(MiddleSelfReferringNode).field("name").had(type=int)))
    old = schemas["2000-01-01"][outer]
    body = {"node": {"name": 1}, "item": {"name": 2}}
    assert old.model_validate(body).model_dump(exclude_unset=True) == body
    with pytest.raises(ValidationError):
        old.model_validate({"node": {"name": 1}, "item": {"name": "latest"}})


def test__generate_versioned_models__bare_generic_whose_nested_models_share_a_qualname__should_validate_old_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(FirstFactoryModel).field("name").had(type=int)))
    old = schemas["2000-01-01"][OuterOfFactoryModels]
    body = {"first": {"name": 1}, "second": {"name": "second", "child": {"name": 2}}}
    assert old.model_validate(body).model_dump(exclude_unset=True) == body
    with pytest.raises(ValidationError):
        old.model_validate({"first": {"name": 1}, "second": {"name": "second", "child": {"name": "latest"}}})


def test__generate_versioned_models__bare_generic_whose_nested_model_has_no_module__should_validate_old_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

    class Envelope(BaseModel, Generic[ItemT]):
        page: Page[ItemT]
        item: ItemT

    Page.__module__ = None  # ty: ignore[invalid-assignment] # Some generated classes have no module.
    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Envelope]
    body = {"page": {"item": {"name": 1}}, "item": {"name": 2}}
    assert old.model_validate(body).model_dump() == body


@pytest.mark.parametrize("changed_item", [typevar_namespace_first.Item, typevar_namespace_second.Item])
def test__generate_versioned_models__bare_generic_sharing_changed_quoted_parameter__should_fail_loudly(
    changed_item: type[BaseModel],
    create_runtime_schemas: CreateRuntimeSchemas,
):
    field = "a" if changed_item is typevar_namespace_first.Item else "b"
    schemas = create_runtime_schemas(version_change(schema(changed_item).field(field).had(type=int)))
    with pytest.raises(SchemaGenerationError) as exc_info:
        schemas["2000-01-01"][typevar_namespace_first.EnvelopeWithItem]
    assert str(exc_info.value) == (
        "Cadwyn cannot version the bare generic EnvelopeWithItem: type parameter T resolves differently across the "
        "namespaces of tests._data.typevar_namespace_first.EnvelopeWithItem, "
        "tests._data.typevar_namespace_second.Page. Use explicit type arguments."
    )


def test__router_generation__typed_extras_with_concrete_annotation__should_validate_and_document_old_model(
    create_versioned_app: CreateVersionedApp,
):
    class Item(BaseModel):
        name: str

    class Page(BaseModel):
        model_config = ConfigDict(extra="allow")
        __pydantic_extra__: dict[str, Item]  # ty: ignore[incompatible-mutable-override] # Pydantic supports typed extras.

    router = VersionedAPIRouter()

    @router.post("/items")
    def create_items(body: Page) -> None:
        pass

    @convert_request_to_next_version_for("/items", ["POST"])
    def migrate(request: RequestInfo) -> None:
        request.body["extra"]["name"] = str(request.body["extra"]["name"])

    app = create_versioned_app(version_change(schema(Item).field("name").had(type=int), migrate=migrate), router=router)
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    assert old.post("/items", json={"extra": {"name": "latest"}}).status_code == 422
    assert old.post("/items", json={"extra": {"name": 1}}).status_code == 200
    schemas = old.get("/openapi.json?version=2000-01-01").json()["components"]["schemas"]
    assert schemas["Item"]["properties"]["name"]["type"] == "integer"


class _TypedExtrasItem(BaseModel):
    name: str


_TypedExtrasItemT = TypeVar("_TypedExtrasItemT", bound=_TypedExtrasItem)
_TypedExtrasOtherT = TypeVar("_TypedExtrasOtherT")


class TypedExtrasPage(BaseModel, Generic[_TypedExtrasOtherT, _TypedExtrasItemT]):
    model_config = ConfigDict(extra="allow")
    other: _TypedExtrasOtherT
    __pydantic_extra__: dict[str, _TypedExtrasItemT]  # ty: ignore[incompatible-mutable-override] # Typed extras.


def test__generate_versioned_models__typed_extras_with_typevar_of_unparametrized_generic__should_validate_old_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(_TypedExtrasItem).field("name").had(type=int)))
    old = schemas["2000-01-01"][TypedExtrasPage]
    assert old.model_validate({"other": 1, "extra": {"name": 1}}).model_dump() == {"other": 1, "extra": {"name": 1}}
    with pytest.raises(ValidationError):
        old.model_validate({"other": 1, "extra": {"name": "latest"}})


def test__generate_versioned_models__typed_extras_with_typevar_of_partially_parametrized_generic__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    schemas = create_runtime_schemas(version_change(schema(_TypedExtrasItem).field("name").had(type=int)))
    # Pydantic versions differ in how they substitute parameters in the typed extras of parametrizations
    with pytest.raises(SchemaGenerationError, match=r"typed extra fields.*_TypedExtrasItemT.*2000-01-01"):
        schemas["2000-01-01"][TypedExtrasPage[int, _TypedExtrasItemT]]


def test__generate_versioned_models__typed_extras_with_typevar_of_non_generic_model__should_validate_old_model(
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel):
        model_config = ConfigDict(extra="allow")
        __pydantic_extra__: dict[str, ItemT]  # ty: ignore[incompatible-mutable-override, unbound-type-variable] # Pydantic supports typed extras.

    old = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))["2000-01-01"][Page]
    assert old.model_validate({"extra": {"name": 1}}).model_dump() == {"extra": {"name": 1}}
    with pytest.raises(ValidationError):
        old.model_validate({"extra": {"name": "latest"}})
