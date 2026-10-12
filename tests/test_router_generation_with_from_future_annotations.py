from __future__ import annotations

import functools
import sys
import types
from enum import Enum
from typing import Annotated, Any

import pytest
from fastapi import Depends, Query, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema
from typing_extensions import Self, override

from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle, VersionChange
from tests._data import postponed_annotations_dependencies as other_module
from tests.conftest import version_change


class OuterSchema(BaseModel):
    bar: MySchema

    extra_annotated: Annotated[str, WithJsonSchema({"type": "string", "description": "Hello"})] = ""


class MySchema(BaseModel):
    foo: str = Field(coerce_numbers_to_str=True)


class MyVersionChange(VersionChange):
    description = "Hello"
    instructions_to_migrate_to_previous_version = (
        schema(MySchema).field("foo").had(type=int),
        schema(MySchema).field("foo").didnt_have("coerce_numbers_to_str"),
    )


app = Cadwyn(versions=VersionBundle(Version("2001-01-01", MyVersionChange), Version("2000-01-01")))
router = VersionedAPIRouter()


@router.post("/test")
async def route_with_inner_schema_forwardref(dep: MySchema) -> MySchema:
    return dep


@router.post("/test2")
async def route_with_outer_schema_forwardref(dep: OuterSchema) -> OuterSchema:
    return dep


app.generate_and_include_versioned_routers(router)


def test__router_generation__using_forwardref_inner_global_schema_in_body():
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test", json={"foo": 1}).json() == {"foo": 1}
    assert client_2001.post("/test", json={"foo": 1}).json() == {"foo": "1"}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == 200
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == 200


def test__router_generation__using_forwardref_outer_global_schema_in_body():
    unversioned_client = TestClient(app)
    client_2000 = TestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"})
    client_2001 = TestClient(app, headers={app.router.api_version_parameter_name: "2001-01-01"})
    assert client_2000.post("/test2", json={"bar": {"foo": 1}}).json() == {"bar": {"foo": 1}, "extra_annotated": ""}
    assert client_2001.post("/test2", json={"bar": {"foo": 1}}).json() == {"bar": {"foo": "1"}, "extra_annotated": ""}
    assert unversioned_client.get("/openapi.json?version=2000-01-01").status_code == 200
    assert unversioned_client.get("/openapi.json?version=2001-01-01").status_code == 200


class CallableClassDependency:
    """A callable class to be used as a dependency (with forward ref annotation due to future annotations)."""

    def __init__(self, label: str):
        super().__init__()
        self.label = label

    async def __call__(self, request: Request) -> None:
        pass


def test__router_generation__using_callable_class_dependency_with_forwardref():
    """Test that callable class instances work as dependencies with future annotations.

    Regression test for https://github.com/zmievsa/cadwyn/issues/321
    """

    class EmptyVersionChange(VersionChange):
        description = "Empty version change"
        instructions_to_migrate_to_previous_version = ()

    test_router = VersionedAPIRouter()

    @test_router.get("/run", dependencies=[Depends(CallableClassDependency("route-level"))])
    async def run():
        return {"status": "ok"}

    test_app = Cadwyn(versions=VersionBundle(Version("2001-01-01", EmptyVersionChange), Version("2000-01-01")))
    test_app.generate_and_include_versioned_routers(test_router)

    client = TestClient(test_app, headers={test_app.router.api_version_parameter_name: "2000-01-01"})
    response = client.get("/run")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class TaskOutcome(str, Enum):
    failed = "failed"
    stopped = "stopped"


class TaskOutcomePayload(BaseModel):
    state: TaskOutcome
    hostname: str | None = None
    pid: int | None = None


@pytest.mark.parametrize("version", ["2001-01-01", "2000-01-01"])
def test__router_generation__forwardref_body_matches_version(router, create_versioned_clients, version):
    @router.post("/outcome")
    def report_outcome(payload: TaskOutcomePayload):
        return payload

    client = create_versioned_clients(
        version_change(
            enum(TaskOutcome).didnt_have("stopped"),
            schema(TaskOutcomePayload).field("hostname").didnt_exist,
            schema(TaskOutcomePayload).field("pid").didnt_exist,
        )
    )[version]
    response = client.get(f"/openapi.json?version={version}")

    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    expected_fields = {"state", "hostname", "pid"} if version == "2001-01-01" else {"state"}
    assert set(schemas["TaskOutcomePayload"]["properties"]) == expected_fields
    expected_outcomes = ["failed", "stopped"] if version == "2001-01-01" else ["failed"]
    assert schemas["TaskOutcome"]["enum"] == expected_outcomes

    response = client.post("/outcome", json={"state": "stopped"})

    assert response.status_code == (200 if version == "2001-01-01" else 422)


@functools.lru_cache
def cached_limit(limit: Annotated[int, Query(ge=1)] = 3) -> int:
    return limit


class DecoratorThatKeepsWrapped:
    def __init__(self, func: Any) -> None:
        functools.update_wrapper(self, func)
        self.func = func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


@DecoratorThatKeepsWrapped
def decorated_limit(request: Request, limit: Annotated[int, Query(ge=1)] = 3) -> int:
    return limit


def test__router_generation__decorated_dependencies_with_forwardrefs__should_resolve_annotations(
    router, create_versioned_clients
):
    @router.get("/cached/annotated")
    async def cached_annotated(limit: Annotated[int, Depends(cached_limit)]):
        return limit

    @router.get("/cached/default")
    async def cached_default(limit: int = Depends(cached_limit)):
        return limit

    @router.get("/decorated/annotated")
    async def decorated_annotated(limit: Annotated[int, Depends(decorated_limit)]):
        return limit

    @router.get("/decorated/default")
    async def decorated_default(limit: int = Depends(decorated_limit)):
        return limit

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/cached/annotated", "/cached/default", "/decorated/annotated", "/decorated/default"):
            assert version_client.get(path, params={"limit": 5}).json() == 5
            assert version_client.get(path, params={"limit": 0}).status_code == 422


class ResourceWithForwardRefsInInit:
    def __init__(self, body: MySchema) -> None:
        self.body = body


class ResourceWithForwardRefsInNew:
    body: MySchema

    def __new__(cls, body: MySchema) -> Self:
        resource = super().__new__(cls)
        resource.body = body
        return resource


class ResourceMeta(type):
    @override
    def __call__(cls, body: MySchema) -> Any:
        return super().__call__(body)


class ResourceWithForwardRefsInMetaclass(metaclass=ResourceMeta):
    def __init__(self, body: Any) -> None:
        self.body = body


class ResourceWithoutConstructor:
    body = MySchema(foo="default")


class HashableBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    foo: str


class ResourceWithCachedNew:
    body: HashableBody

    @functools.lru_cache
    def __new__(cls, body: HashableBody) -> Self:
        resource = super().__new__(cls)
        resource.body = body
        return resource


class ResourceWithCachedInit:
    @functools.lru_cache  # noqa: B019
    def __init__(self, body: HashableBody) -> None:
        self.body = body


class StaticCallMeta(type):
    @override
    @staticmethod
    def __call__(body: HashableBody) -> Any:
        return types.SimpleNamespace(body=body)


class ResourceWithStaticMetaclassCall(metaclass=StaticCallMeta):
    pass


class ClassCallMeta(type):
    @override
    @classmethod
    def __call__(cls, body: HashableBody) -> Any:
        return types.SimpleNamespace(body=body)


class ResourceWithClassMetaclassCall(metaclass=ClassCallMeta):
    pass


def test__router_generation__partial_of_class_with_forwardrefs__should_resolve_annotations(
    router, create_versioned_clients
):
    @router.post("/init")
    async def init_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithForwardRefsInInit))]):
        return resource.body

    @router.post("/new")
    async def new_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithForwardRefsInNew))]):
        return resource.body

    @router.post("/metaclass")
    async def metaclass_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithForwardRefsInMetaclass))],
    ):
        return resource.body

    @router.post("/cached_new")
    async def cached_new_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithCachedNew))]):
        return resource.body

    @router.post("/cached_init")
    async def cached_init_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithCachedInit))]):
        return resource.body

    @router.post("/static_metaclass")
    async def static_metaclass_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithStaticMetaclassCall))],
    ):
        return resource.body

    @router.post("/class_metaclass")
    async def class_metaclass_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithClassMetaclassCall))],
    ):
        return resource.body

    @router.get("/no_constructor")
    async def no_constructor_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithoutConstructor))]):
        return resource.body

    paths = ["/init", "/new", "/metaclass", "/cached_new", "/cached_init"]
    # Python 3.10's inspect.signature() drops the first parameter of a staticmethod or classmethod __call__ of
    # a metaclass so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths += ["/static_metaclass", "/class_metaclass"]
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}
        assert version_client.get("/no_constructor").json() == {"foo": "default"}


def _decorator_from_this_module(func: Any) -> Any:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return wrapper


class ResourceWithPartialmethodInit:
    __init__ = functools.partialmethod(other_module.initialize_with_label, label="fixed")


class ResourceWithStaticmethodNew:
    __new__ = staticmethod(other_module.create)


class ClassmethodCallMeta(type):
    __call__ = classmethod(other_module.metaclass_call)


class ResourceWithClassmethodMetaclassCall(metaclass=ClassmethodCallMeta):
    pass


class ResourceWithDecoratedInit:
    __init__ = _decorator_from_this_module(other_module.initialize)


def test__router_generation__dependencies_with_annotations_from_another_module__should_resolve_them(
    router, create_versioned_clients
):
    @router.post("/partialmethod")
    async def partialmethod_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithPartialmethodInit))]):
        return resource.body

    @router.post("/staticmethod")
    async def staticmethod_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithStaticmethodNew))]):
        return resource.body

    @router.post("/classmethod")
    async def classmethod_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithClassmethodMetaclassCall))],
    ):
        return resource.body

    @router.post("/wrapped")
    async def wrapped_route(resource: Annotated[Any, Depends(functools.partial(ResourceWithDecoratedInit))]):
        return resource.body

    @router.post("/partial")
    async def partial_route(
        body: Annotated[Any, Depends(functools.partial(other_module.get_body_with_label, label="fixed"))],
    ):
        return body

    @router.post("/bound_method")
    async def bound_method_route(body: Annotated[Any, Depends(other_module.provider.provide)]):
        return body

    paths = ["/partialmethod", "/staticmethod", "/wrapped", "/partial", "/bound_method"]
    # Python 3.10's inspect.signature() drops the first parameter of a classmethod __call__ of a metaclass
    # so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths.append("/classmethod")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}
