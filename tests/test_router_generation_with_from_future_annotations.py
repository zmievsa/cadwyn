from __future__ import annotations

import functools
import inspect
import sys
import types
import typing
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Optional

import pytest
from fastapi import Body, Depends, Query, Request
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

if TYPE_CHECKING:
    from decimal import Decimal


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


class InheritedPartialmethodInit(ResourceWithPartialmethodInit):
    pass


class InheritedStaticmethodNew(ResourceWithStaticmethodNew):
    pass


# Each of the following wrappers leads to a function from this module through one attribute and to a function from
# the other module through another one. Only the order of inspect.signature() leads to the annotations it returns.


def _invoke(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


# A partial's explicit __wrapped__ comes before its .func
partial_with_wrapped = functools.update_wrapper(
    functools.partial(_invoke, other_module.get_body), other_module.get_body
)


class ResourceWithPartialmethodOfDecoratedInit:
    __init__ = functools.partialmethod(_decorator_from_this_module(other_module.initialize_with_label), label="fixed")


class ResourceWithStaticmethodOfDecoratedNew:
    __new__ = staticmethod(_decorator_from_this_module(other_module.create))


class ClassmethodOfDecoratedCallMeta(type):
    __call__ = classmethod(_decorator_from_this_module(other_module.metaclass_call))


class ResourceWithClassmethodOfDecoratedMetaclassCall(metaclass=ClassmethodOfDecoratedCallMeta):
    pass


class ProviderWithDecoratedMethod:
    provide = _decorator_from_this_module(other_module.provide)


# The decorator's __wrapped__ leads to a bound method whose function comes from the other module
decorated_bound_method = _decorator_from_this_module(other_module.provider.provide)


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

    @router.post("/inherited_partialmethod")
    async def inherited_partialmethod_route(
        resource: Annotated[Any, Depends(functools.partial(InheritedPartialmethodInit))],
    ):
        return resource.body

    @router.post("/inherited_staticmethod")
    async def inherited_staticmethod_route(
        resource: Annotated[Any, Depends(functools.partial(InheritedStaticmethodNew))],
    ):
        return resource.body

    paths = [
        "/partialmethod",
        "/staticmethod",
        "/wrapped",
        "/partial",
        "/bound_method",
        "/inherited_partialmethod",
        "/inherited_staticmethod",
    ]
    # Python 3.10's inspect.signature() drops the first parameter of a classmethod __call__ of a metaclass
    # so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths.append("/classmethod")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


class _PartialWithOverriddenCall(functools.partial):
    @override
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


_DEPENDENCIES_WITH_NESTED_QUOTED_ANNOTATIONS_FROM_ANOTHER_MODULE = {
    "function": other_module.get_body_with_optional_quoted_body,
    "init": other_module.ResourceWithOptionalQuotedBodyInInit,
    "new": other_module.ResourceWithOptionalQuotedBodyInNew,
    "metaclass_call": other_module.ResourceWithOptionalQuotedBodyInMetaclassCall,
    "partialmethod_init": other_module.ResourceWithOptionalQuotedBodyInPartialmethodInit,
}
# Postponed annotations of routes can only refer to the dependency through the globals
dependency_in_globals: Any = None


_WRAPPERS_OF_DEPENDENCIES_FROM_ANOTHER_MODULE = {
    "partial": functools.partial,
    "partial_subclass": _PartialWithOverriddenCall,
}


@pytest.mark.parametrize("wrapper", list(_WRAPPERS_OF_DEPENDENCIES_FROM_ANOTHER_MODULE))
@pytest.mark.parametrize("shape", list(_DEPENDENCIES_WITH_NESTED_QUOTED_ANNOTATIONS_FROM_ANOTHER_MODULE))
def test__router_generation__dependency_from_another_module_with_nested_quoted_annotations__should_resolve_them(
    router, create_versioned_clients, monkeypatch, shape, wrapper
):
    dependency = _WRAPPERS_OF_DEPENDENCIES_FROM_ANOTHER_MODULE[wrapper](
        _DEPENDENCIES_WITH_NESTED_QUOTED_ANNOTATIONS_FROM_ANOTHER_MODULE[shape]
    )
    monkeypatch.setitem(globals(), "dependency_in_globals", dependency)

    @router.post("/default")
    async def default_route(body: Any = Depends(dependency_in_globals)):
        raise NotImplementedError

    @router.post("/annotated")
    async def annotated_route(body: Annotated[Any, Depends(dependency_in_globals)]):
        raise NotImplementedError

    clients = create_versioned_clients(version_change(schema(other_module.Payload).field("foo").had(type=int)))
    # FastAPI itself cannot evaluate the forward references within the annotations of partials in the head route
    # that Cadwyn validates requests against after migrating them so we only check what each version validates
    # and documents on its own
    for path in ("/default", "/annotated"):
        response = clients["2000-01-01"].post(path, json={"body": {"foo": "bar"}, "other": None})
        assert [(error["loc"], error["type"]) for error in response.json()["detail"]] == [
            (["body", "body", "foo"], "int_parsing")
        ]
    foo_types = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        foo_types[version] = openapi["components"]["schemas"]["Payload"]["properties"]["foo"]["type"]
    assert foo_types == {"2001-01-01": "string", "2000-01-01": "integer"}


class PartialSubclassPayload(BaseModel):
    foo: str


class ResourceWithOptionalQuotedBody:
    def __init__(self, body: Optional["PartialSubclassPayload"]) -> None:  # noqa: UP037
        raise NotImplementedError


partial_subclass_of_class = _PartialWithOverriddenCall(ResourceWithOptionalQuotedBody)


def test__router_generation__partial_subclass_of_class_with_nested_quoted_annotations__should_resolve_them(
    router, create_versioned_clients
):
    @router.post("/test")
    async def route(resource: Annotated[Any, Depends(partial_subclass_of_class)]):
        raise NotImplementedError

    clients = create_versioned_clients(
        version_change(schema(PartialSubclassPayload).field("foo").had(type=int)),
    )
    response = clients["2000-01-01"].post("/test", json={"foo": "bar"})
    assert [(error["loc"], error["type"]) for error in response.json()["detail"]] == [(["body", "foo"], "int_parsing")]
    foo_types = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        foo_types[version] = openapi["components"]["schemas"]["PartialSubclassPayload"]["properties"]["foo"]["type"]
    assert foo_types == {"2001-01-01": "string", "2000-01-01": "integer"}


# other_module has a namesake of this alias
Number = int


class _PartialWithOverriddenCallAndSignature(functools.partial):
    @override
    def __call__(self, number: Number) -> Any:
        raise NotImplementedError

    __signature__ = inspect.signature(__call__).replace(
        parameters=list(inspect.signature(__call__).parameters.values())[1:]
    )


@pytest.mark.parametrize("func", [int, other_module.get_number])
def test__router_generation__partial_subclass_with_signature_of_its_call__should_resolve_it_in_the_globals_of_call(
    router, create_versioned_clients, monkeypatch, func
):
    monkeypatch.setitem(globals(), "dependency_in_globals", _PartialWithOverriddenCallAndSignature(func))

    @router.get("/test")
    async def route(number: Annotated[Any, Depends(dependency_in_globals)]):
        raise NotImplementedError

    for version, version_client in create_versioned_clients(version_change()).items():
        response = version_client.get("/test", params={"number": "five"})
        assert [(error["loc"], error["type"]) for error in response.json()["detail"]] == [
            (["query", "number"], "int_parsing")
        ]
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        assert openapi["paths"]["/test"]["get"]["parameters"][0]["schema"]["type"] == "integer"


def test__router_generation__wrapped_dependencies_from_another_module__should_resolve_annotations_like_signature(
    router, create_versioned_clients
):
    @router.post("/partial_with_wrapped")
    async def partial_with_wrapped_route(body: Annotated[Any, Depends(partial_with_wrapped)]):
        return body

    @router.post("/partialmethod_of_decorated")
    async def partialmethod_of_decorated_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithPartialmethodOfDecoratedInit))],
    ):
        return resource.body

    @router.post("/staticmethod_of_decorated")
    async def staticmethod_of_decorated_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithStaticmethodOfDecoratedNew))],
    ):
        return resource.body

    @router.post("/classmethod_of_decorated")
    async def classmethod_of_decorated_route(
        resource: Annotated[Any, Depends(functools.partial(ResourceWithClassmethodOfDecoratedMetaclassCall))],
    ):
        return resource.body

    @router.post("/bound_method_of_decorated")
    async def bound_method_of_decorated_route(body: Annotated[Any, Depends(ProviderWithDecoratedMethod().provide)]):
        return body

    @router.post("/decorated_bound_method")
    async def decorated_bound_method_route(body: Annotated[Any, Depends(decorated_bound_method)]):
        return body

    paths = [
        "/partial_with_wrapped",
        "/partialmethod_of_decorated",
        "/staticmethod_of_decorated",
        "/bound_method_of_decorated",
        "/decorated_bound_method",
    ]
    # Python 3.10's inspect.signature() drops the first parameter of a classmethod __call__ of a metaclass
    # so even FastAPI itself cannot use such dependencies there
    if sys.version_info >= (3, 11):
        paths.append("/classmethod_of_decorated")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


class ProviderWithPropertyCall:
    def __init__(self, function: Any) -> None:
        self.function = function

    @property
    def __call__(self) -> Any:
        return self.function


class _InstanceFunctionDescriptor:
    def __get__(self, instance: Any, owner: type) -> Any:
        return instance.function


class ProviderWithDescriptorCall:
    __call__ = _InstanceFunctionDescriptor()

    def __init__(self, function: Any) -> None:
        self.function = function


class ProviderWithStaticmethodCall:
    __call__ = staticmethod(other_module.get_body)


class ProviderWithClassmethodCall:
    __call__ = classmethod(other_module.provide_with_class)


class ProviderWithPartialmethodCall:
    __call__ = functools.partialmethod(other_module.provide_with_label, label="fixed")


class ProviderWithInstanceCall:
    # A callable instance without __get__ gets called as is
    __call__ = other_module.callable_provider


class ProviderWithClassCall:
    # Calling the instance constructs the class
    __call__ = other_module.Resource


class FunctionLike:
    """A duck type of a function (e.g. a function compiled with Cython) that inspect.signature() reads directly"""

    def __init__(self, function: Any) -> None:
        self.function = function
        self.__name__ = function.__name__
        self.__code__ = function.__code__
        self.__defaults__ = function.__defaults__
        self.__kwdefaults__ = function.__kwdefaults__
        self.__annotations__ = function.__annotations__
        self.__globals__ = function.__globals__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.function(*args, **kwargs)


property_provider = ProviderWithPropertyCall(other_module.get_body)
descriptor_provider = ProviderWithDescriptorCall(other_module.get_body)
staticmethod_provider = ProviderWithStaticmethodCall()
classmethod_provider = ProviderWithClassmethodCall()
partialmethod_provider = ProviderWithPartialmethodCall()
instance_call_provider = ProviderWithInstanceCall()
class_call_provider = ProviderWithClassCall()
function_like = FunctionLike(other_module.get_body)

# A partial that also carries the metadata of a function
function_like_partial = functools.partial(_invoke, other_module.get_body)
for _attribute_name in ("__name__", "__code__", "__defaults__", "__kwdefaults__", "__annotations__", "__globals__"):
    setattr(function_like_partial, _attribute_name, getattr(other_module.get_body, _attribute_name))


def test__router_generation__callable_instances_with_descriptor_call__should_resolve_annotations_like_signature(
    router, create_versioned_clients
):
    @router.post("/partialmethod")
    async def partialmethod_route(body: Annotated[Any, Depends(partialmethod_provider)]):
        return body

    paths = ["/partialmethod"]
    # Python 3.10's inspect.signature() looks up __call__ on the class instead of the instance
    # so it fails or drops the first parameter for these dependencies and even FastAPI itself cannot use them there
    if sys.version_info >= (3, 11):

        @router.post("/staticmethod")
        async def staticmethod_route(body: Annotated[Any, Depends(staticmethod_provider)]):
            return body

        @router.post("/classmethod")
        async def classmethod_route(body: Annotated[Any, Depends(classmethod_provider)]):
            return body

        @router.post("/instance")
        async def instance_route(body: Annotated[Any, Depends(instance_call_provider)]):
            return body

        @router.post("/class")
        async def class_route(resource: Annotated[Any, Depends(class_call_provider)]):
            return resource.body

        @router.post("/property")
        async def property_route(body: Annotated[Any, Depends(property_provider)]):
            return body

        @router.post("/descriptor")
        async def descriptor_route(body: Annotated[Any, Depends(descriptor_provider)]):
            return body

        paths += ["/staticmethod", "/classmethod", "/instance", "/class", "/property", "/descriptor"]
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


def test__router_generation__function_like_dependency__should_resolve_annotations_in_its_globals(
    router, create_versioned_clients
):
    @router.post("/function_like")
    async def function_like_route(body: Annotated[Any, Depends(function_like)]):
        return body

    paths = ["/function_like"]
    # Python 3.10-3.12's inspect.signature() reads a function-like partial directly. Python 3.13+ unwraps partials
    # first so it gets the signature of _invoke which has no "body" and even FastAPI itself cannot use it there
    if sys.version_info < (3, 13):

        @router.post("/function_like_partial")
        async def function_like_partial_route(body: Annotated[Any, Depends(function_like_partial)]):
            return body

        paths.append("/function_like_partial")
    for version_client in create_versioned_clients(version_change()).values():
        for path in paths:
            response = version_client.post(path, json={"foo": "bar"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == {"foo": "bar"}


def get_limit_with_type_checking_only_return(limit: int) -> Decimal:
    return limit  # ty: ignore[invalid-return-type]  # The annotation is only for type checkers.


def get_limit_with_type_checking_only_parameter(limit: int, price: Decimal | None = None) -> int:
    return limit


class LimitGetterWithTypeCheckingOnlyReturn:
    def __call__(self, limit: int) -> Decimal:
        return limit  # ty: ignore[invalid-return-type]  # The annotation is only for type checkers.


limit_getter_with_type_checking_only_return = LimitGetterWithTypeCheckingOnlyReturn()


def test__router_generation__dependency_with_type_checking_only_annotations__should_resolve_the_other_ones(
    router, create_versioned_clients
):
    @router.get("/return")
    async def return_route(limit: Annotated[int, Depends(get_limit_with_type_checking_only_return)]):
        return limit

    @router.get("/parameter")
    async def parameter_route(limit: Annotated[int, Depends(get_limit_with_type_checking_only_parameter)]):
        return limit

    @router.get("/instance")
    async def instance_route(limit: Annotated[int, Depends(limit_getter_with_type_checking_only_return)]):
        return limit

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/return", "/parameter", "/instance"):
            response = version_client.get(path, params={"limit": "5"})
            assert response.status_code == 200, (path, response.json())
            assert response.json() == 5
            assert version_client.get(path, params={"limit": "five"}).status_code == 422


class VersionedItem(BaseModel):
    foo: str


def get_item_with_type_checking_only_return(item: VersionedItem) -> Decimal:
    raise NotImplementedError


def test__router_generation__partial_dependency_with_type_checking_only_annotations__should_use_versioned_models(
    router, create_versioned_clients
):
    # FastAPI itself cannot resolve "VersionedItem" for such partials at request time (it looks for the globals on
    # the partial) so we only compare the schemas that each version documents
    @router.post("/test")
    async def route(item: Annotated[Any, Depends(functools.partial(get_item_with_type_checking_only_return))]):
        raise NotImplementedError

    clients = create_versioned_clients(version_change(schema(VersionedItem).field("bar").existed_as(type=str)))
    required_fields = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["VersionedItem"]["required"]
    assert required_fields == {"2001-01-01": ["foo"], "2000-01-01": ["foo", "bar"]}


class NotSubscriptableAtRuntime:
    """Like pandas.Series: its stubs make it generic for type checkers but subscripting it fails at runtime"""

    def __class_getitem__(cls, item: Any) -> Any:  # pragma: no cover # Only runs if something evaluates the annotation
        raise TypeError(f"type {cls.__name__!r} is not subscriptable")


def make_values(size: int) -> NotSubscriptableAtRuntime[int]:  # ty: ignore[invalid-type-form]  # Like pandas.
    return list(range(size))


class ModelWithCallables(BaseModel):
    values: Any = Field(default_factory=functools.partial(make_values, 3))
    size: Annotated[int, functools.partial(make_values, 2)] = 1


def test__router_generation__non_dependency_callables_with_unevaluable_annotations__should_work(
    router, create_versioned_clients
):
    # FastAPI never evaluates the annotations of callables other than endpoints and dependencies so neither does Cadwyn
    @router.post("/test")
    async def route(body: ModelWithCallables):
        return body

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.post("/test", json={}).json() == {"values": [0, 1, 2], "size": 1}


# With postponed annotations, the quotes make this a string within a string so evaluating it once leaves a string
def make_values_with_quoted_return(
    size: int = 3,
) -> "NotSubscriptableAtRuntime[int]":  # noqa: UP037  # ty: ignore[invalid-type-form]
    return list(range(size))


cached_make_values_with_quoted_return = functools.lru_cache(make_values_with_quoted_return)


def test__router_generation__dependency_with_quoted_unevaluable_return_annotation__should_work(
    router, create_versioned_clients
):
    # FastAPI never evaluates the return annotations of dependencies so neither does Cadwyn
    @router.get("/partial")
    async def partial_route(values: Annotated[Any, Depends(functools.partial(make_values_with_quoted_return, 3))]):
        return values

    @router.get("/cached")
    async def cached_route(values: Annotated[Any, Depends(cached_make_values_with_quoted_return)]):
        return values

    for version_client in create_versioned_clients(version_change()).values():
        for path in ("/partial", "/cached"):
            assert version_client.get(path).json() == [0, 1, 2], path


def test__router_generation__function_dependency_with_quoted_unevaluable_return_annotation__should_work(
    router, create_versioned_clients
):
    @router.get("/test")
    async def route(values: Annotated[Any, Depends(make_values_with_quoted_return)]):
        return values

    for version_client in create_versioned_clients(version_change()).values():
        assert version_client.get("/test").json() == [0, 1, 2]


class QuotedPayload(BaseModel):
    current: str


# With postponed annotations, the quotes make these strings within strings so evaluating them once leaves strings
def get_current_from_quoted_body(body: "QuotedPayload") -> str:  # noqa: UP037
    return body.current


@pytest.mark.parametrize("style", ["endpoint", "default_dependency", "annotated_dependency"])
def test__router_generation__quoted_parameter_annotations__should_use_versioned_models(
    router, create_versioned_clients, style
):
    if style == "endpoint":

        @router.post("/test")
        async def endpoint_route(body: "QuotedPayload"):  # noqa: UP037
            return body.current

    elif style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(current: str = Depends(get_current_from_quoted_body)):
            return current

    else:

        @router.post("/test")
        async def annotated_dependency_route(current: Annotated[str, Depends(get_current_from_quoted_body)]):
            return current

    clients = create_versioned_clients(version_change(schema(QuotedPayload).field("legacy").existed_as(type=str)))
    assert clients["2001-01-01"].post("/test", json={"current": "ok"}).json() == "ok"
    assert clients["2000-01-01"].post("/test", json={"current": "ok"}).status_code == 422
    assert clients["2000-01-01"].post("/test", json={"current": "ok", "legacy": "ok"}).json() == "ok"
    required_fields = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["QuotedPayload"]["required"]
    assert required_fields == {"2001-01-01": ["current"], "2000-01-01": ["current", "legacy"]}


# With postponed annotations, evaluating these once leaves forward references within them
def get_current_from_nested_quoted_list(items: list["QuotedPayload"]) -> str:  # noqa: UP037
    raise NotImplementedError


def get_current_from_nested_quoted_typing_list(items: typing.List["QuotedPayload"]) -> str:  # noqa: UP006, UP037
    raise NotImplementedError


def get_current_from_nested_quoted_optional(item: Optional["QuotedPayload"]) -> str:  # noqa: UP037
    raise NotImplementedError


def get_current_from_nested_quoted_dict(items: dict[str, "QuotedPayload"]) -> str:  # noqa: UP037
    raise NotImplementedError


def get_current_from_nested_quoted_annotated(items: Annotated[list["QuotedPayload"], Body()]) -> str:  # noqa: UP037
    raise NotImplementedError


_NESTED_QUOTED_DEPENDENCIES = {
    "list": (get_current_from_nested_quoted_list, lambda payload: [payload]),
    "typing_list": (get_current_from_nested_quoted_typing_list, lambda payload: [payload]),
    "optional": (get_current_from_nested_quoted_optional, lambda payload: payload),
    "dict": (get_current_from_nested_quoted_dict, lambda payload: {"key": payload}),
    "annotated": (get_current_from_nested_quoted_annotated, lambda payload: [payload]),
}


# Postponed annotations of routes can only refer to the dependency through the globals
nested_quoted_dependency: Any = None


@pytest.mark.parametrize("shape", list(_NESTED_QUOTED_DEPENDENCIES))
@pytest.mark.parametrize("style", ["endpoint", "default_dependency", "annotated_dependency"])
def test__router_generation__nested_quoted_parameter_annotations__should_use_versioned_models(
    router, create_versioned_clients, monkeypatch, style, shape
):
    if sys.version_info < (3, 11) and shape in {"list", "dict", "annotated"}:
        pytest.skip("Python 3.10 cannot evaluate forward references within builtin generics such as list['Model']")
    dependency, make_body = _NESTED_QUOTED_DEPENDENCIES[shape]
    monkeypatch.setitem(globals(), "nested_quoted_dependency", dependency)
    if style == "endpoint":
        router.post("/test")(dependency)
    elif style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(current: str = Depends(nested_quoted_dependency)):
            raise NotImplementedError

    else:

        @router.post("/test")
        async def annotated_dependency_route(current: Annotated[str, Depends(nested_quoted_dependency)]):
            raise NotImplementedError

    clients = create_versioned_clients(version_change(schema(QuotedPayload).field("legacy").existed_as(type=str)))
    # FastAPI itself cannot evaluate such forward references in the head route that Cadwyn validates requests
    # against after migrating them so we only check what each version validates and documents on its own
    assert clients["2000-01-01"].post("/test", json=make_body({"current": "ok"})).status_code == 422
    required_fields = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["QuotedPayload"]["required"]
    assert required_fields == {"2001-01-01": ["current"], "2000-01-01": ["current", "legacy"]}


def _with_signature_of_unevaluated_annotations(function: Any) -> Any:
    # Like decorators that rebuild the signature to inject parameters or defaults (e.g. fastapi-utils' @cbv).
    # inspect.signature() leaves postponed annotations as strings and eval_str does not evaluate __signature__
    function.__signature__ = inspect.signature(function)
    return function


@_with_signature_of_unevaluated_annotations
def get_current_from_body_with_unevaluated_signature(body: QuotedPayload) -> str:
    return body.current


@pytest.mark.parametrize("style", ["endpoint", "default_dependency", "annotated_dependency"])
def test__router_generation__signatures_with_unevaluated_annotations__should_use_versioned_models(
    router, create_versioned_clients, style
):
    if style == "endpoint":

        @router.post("/test")
        @_with_signature_of_unevaluated_annotations
        async def endpoint_route(body: QuotedPayload):
            return body.current

    elif style == "default_dependency":

        @router.post("/test")
        async def default_dependency_route(current: str = Depends(get_current_from_body_with_unevaluated_signature)):
            return current

    else:

        @router.post("/test")
        async def annotated_dependency_route(
            current: Annotated[str, Depends(get_current_from_body_with_unevaluated_signature)],
        ):
            return current

    clients = create_versioned_clients(version_change(schema(QuotedPayload).field("legacy").existed_as(type=str)))
    assert clients["2001-01-01"].post("/test", json={"current": "ok"}).json() == "ok"
    assert clients["2000-01-01"].post("/test", json={"current": "ok"}).status_code == 422
    assert clients["2000-01-01"].post("/test", json={"current": "ok", "legacy": "ok"}).json() == "ok"
    required_fields = {}
    for version, version_client in clients.items():
        openapi = version_client.get(f"/openapi.json?version={version}").json()
        required_fields[version] = openapi["components"]["schemas"]["QuotedPayload"]["required"]
    assert required_fields == {"2001-01-01": ["current"], "2000-01-01": ["current", "legacy"]}


def make_values_with_annotations_set_by_hand() -> Any:
    return [0, 1, 2]


make_values_with_annotations_set_by_hand.__annotations__ = {"return": "NotSubscriptableAtRuntime[int]"}


class ModelWithFactoryWithAnnotationsSetByHand(BaseModel):
    values: Any = Field(default_factory=make_values_with_annotations_set_by_hand)


def test__router_generation__default_factory_with_annotations_set_by_hand__should_read_them_like_before(
    router, create_versioned_clients
):
    @router.post("/test")
    async def route(body: ModelWithFactoryWithAnnotationsSetByHand):
        return body.values

    # Cadwyn reads the annotations of such callables through annotationlib on Python 3.14+ which ignores the ones
    # set by hand. Before 3.14, it reads __annotations__ and evaluates them
    if sys.version_info >= (3, 14):
        for version_client in create_versioned_clients(version_change()).values():
            assert version_client.post("/test", json={}).json() == [0, 1, 2]
    else:
        with pytest.raises(TypeError, match="NotSubscriptableAtRuntime"):
            create_versioned_clients(version_change())
