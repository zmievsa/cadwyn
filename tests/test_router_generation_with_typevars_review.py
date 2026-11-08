import sys
import types
from typing import Annotated, Any, Generic, TypeVar, Union

import pytest
from fastapi import Body
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, ValidationError
from typing_extensions import TypeAliasType, override

from cadwyn import RequestInfo, VersionedAPIRouter, convert_request_to_next_version_for, schema
from cadwyn.exceptions import SchemaGenerationError
from tests.conftest import CreateRuntimeSchemas, CreateVersionedApp, version_change


@pytest.mark.parametrize("fallback", ["bound='Node'", "bound=Union['Node', int]", "'Node', int", "default='Node'"])
def test__router_generation__self_bound_typevar__should_preserve_recursive_requests_and_openapi(
    fallback: str,
    create_versioned_app: CreateVersionedApp,
    monkeypatch: pytest.MonkeyPatch,
):
    module = types.ModuleType("_cadwyn_selfbound_review")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(
        "from typing import Generic, Union\n"
        "from typing_extensions import TypeVar\n"
        "from pydantic import BaseModel\n"
        f"T = TypeVar('T', {fallback})\n"
        "class Node(BaseModel, Generic[T]):\n    child: Union[T, None] = None\n    name: str\n"
        "Node.model_rebuild()\n",
        module.__dict__,
    )
    node = module.__dict__["Node"]
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
        spec = client.get(f"/openapi.json?version={version}").json()
        nodes = [value for value in spec["components"]["schemas"].values() if value.get("title") == "Node"]
        assert nodes
        assert all(
            value["properties"]["name"].get("description") == ("old" if version == "2000-01-01" else None)
            for value in nodes
        )


@pytest.mark.parametrize("concrete_annotation", [False, True])
def test__router_generation__typed_extras__should_version_concrete_annotations_but_keep_typevar_limitation(
    concrete_annotation: bool,
    create_versioned_app: CreateVersionedApp,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        model_config = ConfigDict(extra="allow")
        __pydantic_extra__: dict[str, ItemT]  # ty: ignore[invalid-mutable-override] # Pydantic supports typed extras.

    class ConcretePage(BaseModel):
        model_config = ConfigDict(extra="allow")
        __pydantic_extra__: dict[str, Item]  # ty: ignore[invalid-mutable-override] # Pydantic supports typed extras.

    router = VersionedAPIRouter()

    def exchange(body: Any) -> None:
        pass

    exchange.__annotations__["body"] = ConcretePage if concrete_annotation else Page
    router.post("/items")(exchange)

    @convert_request_to_next_version_for("/items", ["POST"])
    def migrate(request: RequestInfo) -> None:
        request.body["extra"]["name"] = str(request.body["extra"]["name"])

    app = create_versioned_app(version_change(schema(Item).field("name").had(type=int), migrate=migrate), router=router)
    old = TestClient(app, headers={"x-api-version": "2000-01-01"})
    latest = TestClient(app, headers={"x-api-version": "2001-01-01"})
    assert old.post("/items", json={"extra": {"name": "latest"}}).status_code == (422 if concrete_annotation else 200)
    assert old.post("/items", json={"extra": {"name": 1}}).status_code == (200 if concrete_annotation else 422)
    assert latest.post("/items", json={"extra": {"name": "latest"}}).status_code == 200
    old_type = "integer" if concrete_annotation else "string"
    for client, version, expected_type in ((old, "2000-01-01", old_type), (latest, "2001-01-01", "string")):
        spec = client.get(f"/openapi.json?version={version}").json()
        schemas = spec["components"]["schemas"]
        page_ref = spec["paths"]["/items"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        item_ref = schemas[page_ref.rsplit("/", 1)[-1]]["additionalProperties"]["$ref"]
        assert schemas[item_ref.rsplit("/", 1)[-1]]["properties"]["name"]["type"] == expected_type


@pytest.mark.parametrize("parametrized", [False, True])
def test__router_generation__recursive_generic_with_bound__should_validate_each_version(
    parametrized: bool,
    create_versioned_app: CreateVersionedApp,
    monkeypatch: pytest.MonkeyPatch,
):
    module = types.ModuleType("_cadwyn_review_recursive")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(
        "from typing import TypeVar, Generic\n"
        "from pydantic import BaseModel\n"
        "class Item(BaseModel):\n    name: str\n"
        "T = TypeVar('T', bound=Item)\n"
        "class Node(BaseModel, Generic[T]):\n    item: T\n    children: list['Node[T]'] = []\n",
        module.__dict__,
    )
    item, node = module.__dict__["Item"], module.__dict__["Node"]
    node = node[item] if parametrized else node
    router = VersionedAPIRouter()

    def exchange(payload: Any) -> Any:
        return payload

    exchange.__annotations__["payload"] = node
    router.post("/node", response_model=node)(exchange)
    app = create_versioned_app(version_change(schema(item).field("name").had(description="Old")), router=router)
    body = {"item": {"name": "A"}, "children": [{"item": {"name": "B"}}]}
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/node", json=body)
        assert response.status_code == 200
        assert response.json() == {"item": {"name": "A"}, "children": [{"item": {"name": "B"}, "children": []}]}
        assert client.get(f"/openapi.json?version={version}").status_code == 200


@pytest.mark.parametrize("generic_has_instructions", [False, True])
def test__router_generation__fully_parametrized_generic_with_recursive_bound__should_ignore_bound(
    generic_has_instructions: bool,
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

    router = VersionedAPIRouter()

    @router.post("/items", response_model=Payload[int])
    def exchange(payload: Payload[int]) -> Any:
        return payload

    instructions = [schema(Node).field("name").had(type=int)]
    if generic_has_instructions:
        instructions.append(schema(Payload).field("page").had(description="Old"))
    app = create_versioned_app(version_change(*instructions), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/items", json={"page": 1})
        assert response.status_code == 200
        assert response.json() == {"page": 1}
        spec = client.get(f"/openapi.json?version={version}").json()
        assert not any(value.get("title") == "Node" for value in spec["components"]["schemas"].values())


def test__router_generation__nested_concrete_generic_with_recursive_bound__should_ignore_bound(
    create_versioned_app: CreateVersionedApp,
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

    router = VersionedAPIRouter()

    @router.post("/items", response_model=Outer[int])
    def exchange(payload: Outer[int]) -> Any:
        return payload

    app = create_versioned_app(version_change(schema(Node).field("name").had(type=int)), router=router)
    for version in ("2000-01-01", "2001-01-01"):
        client = TestClient(app, headers={"x-api-version": version})
        response = client.post("/items", json={"inner": {"value": 1}})
        assert response.status_code == 200
        assert response.json() == {"inner": {"value": 1}}


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


def test__generate_versioned_models__recursive_parameter_fallbacks__should_fail_loudly(
    create_runtime_schemas: CreateRuntimeSchemas,
    monkeypatch: pytest.MonkeyPatch,
):
    module = types.ModuleType("_cadwyn_recursive_parameters")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(
        "from typing import TypeVar\n"
        "from typing_extensions import TypeAliasType\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "FirstT = TypeVar('FirstT', bound='SecondT')\n"
        "SecondT = TypeVar('SecondT', bound=FirstT)\n"
        "alias = TypeAliasType('alias', list[FirstT], type_params=(FirstT,))\n"
        "class Envelope(BaseModel):\n"
        "    item: int\n",
        module.__dict__,
    )
    envelope = module.__dict__["Envelope"]

    with pytest.raises(SchemaGenerationError, match=r"type parameter FirstT.*refer back to it"):
        create_runtime_schemas(version_change(schema(envelope).field("item").had(type=module.__dict__["alias"])))


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


@pytest.mark.parametrize("reentrant", [False, True])
def test__generate_versioned_models__bare_generic_overriding_parametrization__should_fail_loudly(
    reentrant: bool,
    create_runtime_schemas: CreateRuntimeSchemas,
):
    class Item(BaseModel):
        name: str

    ItemT = TypeVar("ItemT", bound=Item)

    class Page(BaseModel, Generic[ItemT]):
        item: ItemT

        @override
        def __class_getitem__(cls, typevar_values: Any) -> Any:
            return schemas["2000-01-01"][cls] if reentrant else int

    schemas = create_runtime_schemas(version_change(schema(Item).field("name").had(type=int)))
    message = "fallbacks refer back to it" if reentrant else "cannot safely parametrize the bare generic"
    with pytest.raises(SchemaGenerationError, match=message):
        schemas["2000-01-01"][Page]


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


@pytest.mark.parametrize("has_changes", [False, True])
def test__router_generation__shared_typevar_in_different_modules__should_resolve_each_namespace(
    has_changes: bool,
    create_versioned_app: CreateVersionedApp,
    monkeypatch: pytest.MonkeyPatch,
):
    shared_t = TypeVar("shared_t", bound="Item")  # ty: ignore[unresolved-reference] # Resolved separately in each module. # noqa: F821
    pages = []
    items = []
    for letter in ("a", "b"):
        module = types.ModuleType(f"_cadwyn_shared_typevar_{letter}")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        module.__dict__.update(T=shared_t, BaseModel=BaseModel, Generic=Generic)
        exec(
            f"class Item(BaseModel):\n    {letter}: str\n"
            f"class Page{letter.upper()}(BaseModel, Generic[T]):\n    item: T",
            module.__dict__,
        )
        pages.append(module.__dict__[f"Page{letter.upper()}"])
        items.append(module.__dict__["Item"])
    page_a, page_b = pages

    class Payload(BaseModel):
        a: page_a  # ty: ignore[invalid-type-form] # Runtime module classes.
        b: page_b  # ty: ignore[invalid-type-form] # Runtime module classes.

    router = VersionedAPIRouter()

    @router.post("/items", response_model=Payload)
    def exchange(payload: Payload) -> Any:
        return payload

    change = version_change(schema(items[0]).field("a").had(description="Old")) if has_changes else version_change()
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


@pytest.mark.parametrize("changed_namespace", ["a", "b", "unrelated"])
@pytest.mark.parametrize("both_namespaces", [False, True])
def test__router_generation__bare_generic_sharing_quoted_parameter_across_namespaces__should_fail_loudly(
    changed_namespace: str,
    both_namespaces: bool,
    create_runtime_schemas: CreateRuntimeSchemas,
    monkeypatch: pytest.MonkeyPatch,
):
    shared_t = TypeVar("shared_t", bound="Item")  # ty: ignore[unresolved-reference] # Module-specific bound. # noqa: F821
    modules = []
    for letter in ("a", "b"):
        module = types.ModuleType(f"_cadwyn_nested_namespace_{letter}")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        module.__dict__.update(T=shared_t, BaseModel=BaseModel, Generic=Generic)
        exec(
            f"class Item(BaseModel):\n    {letter}: str\nclass Page(BaseModel, Generic[T]):\n    item: T",
            module.__dict__,
        )
        modules.append(module)
    modules[0].__dict__["OtherPage"] = modules[1].__dict__["Page"]
    exec(
        "class Envelope(BaseModel, Generic[T]):\n    page: OtherPage[T]" + ("\n    item: T" if both_namespaces else ""),
        modules[0].__dict__,
    )

    class Unrelated(BaseModel):
        unrelated: str

    changed = (
        Unrelated
        if changed_namespace == "unrelated"
        else modules[0 if changed_namespace == "a" else 1].__dict__["Item"]
    )
    schemas = create_runtime_schemas(version_change(schema(changed).field(changed_namespace).had(type=int)))
    if both_namespaces and changed_namespace != "unrelated":
        with pytest.raises(SchemaGenerationError, match=r"namespaces.*explicit type arguments"):
            schemas["2000-01-01"][modules[0].__dict__["Envelope"]]
    else:
        old = schemas["2000-01-01"][modules[0].__dict__["Envelope"]]
        body = {"page": {"item": {"b": 1 if changed_namespace == "b" else "B"}}}
        if both_namespaces:
            body["item"] = {"a": "A"}
        assert old.model_validate(body).model_dump() == body


@pytest.mark.parametrize("usage", ["classvar", "factory_annotation"])
def test__router_generation__bare_generic_in_unvalidated_metadata__should_ignore_fallback(
    usage: str,
    create_versioned_app: CreateVersionedApp,
):
    from typing import ClassVar

    from pydantic import Field

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


def test__router_generation__parametrized_alias_without_fallbacks__should_keep_main_openapi(
    create_versioned_app: CreateVersionedApp,
):
    from typing_extensions import TypeAliasType

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
