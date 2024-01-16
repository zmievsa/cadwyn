import http.cookies
import re
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from datetime import date
from io import StringIO
from types import ModuleType
from typing import Annotated, Any, Literal, get_args

import pytest
from dirty_equals import IsPartialDict, IsStr
from fastapi import APIRouter, Body, Cookie, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.responses import StreamingResponse

from cadwyn import VersionedAPIRouter
from cadwyn._compat import PYDANTIC_V2, model_dump
from cadwyn.exceptions import CadwynStructureError
from cadwyn.routing import InternalRepresentationOf
from cadwyn.structure import (
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
)
from cadwyn.structure.data import RequestInfo, ResponseInfo
from tests.conftest import (
    CreateVersionedClients,
    LatestModuleFor,
    client,
    version_change,
)


@pytest.fixture()
def test_path():
    return "/test"


@pytest.fixture(autouse=True)
def latest_module(latest_module_for: LatestModuleFor):
    if PYDANTIC_V2:
        return latest_module_for(
            """
    from pydantic import BaseModel, Field, RootModel
    from typing import Any

        # so `RootModel[Any] is RootModel[Any]`. This causes dire consequences if you try to make "different"
        # request and response root models with the same definitions
    class AnyRequestSchema(RootModel[Any]):
        pass

    class AnyResponseSchema(RootModel[Any]):
        pass

    class SchemaWithInternalRepresentation(BaseModel):
        foo: int

    class InternalSchema(SchemaWithInternalRepresentation):
        bar: str | None = Field(default=None)


            """,
        )
    else:
        return latest_module_for(
            """
    from pydantic import BaseModel, Field
    from typing import Any

    class AnyRequestSchema(BaseModel):
        __root__: Any


    class AnyResponseSchema(BaseModel):
        __root__: Any

    class SchemaWithInternalRepresentation(BaseModel):
        foo: int

    class InternalSchema(SchemaWithInternalRepresentation):
        bar: str | None = Field(default=None)


            """,
        )


@pytest.fixture(params=["is_async", "is_sync"])
def _get_endpoint(
    request: pytest.FixtureRequest,
    test_path: str,
    router: VersionedAPIRouter,
    latest_module: ModuleType,
):
    def _get_response_data(request: Request):
        return {
            "headers": dict(request.headers),
            "cookies": request.cookies,
            "query_params": dict(request.query_params),
        }

    if request.param == "is_async":

        @router.get(test_path, response_model=latest_module.AnyResponseSchema)
        async def get_async_endpoint(request: Request):
            return _get_response_data(request)

    else:

        @router.get(test_path, response_model=latest_module.AnyResponseSchema)
        def get_sync_endpoint(request: Request):
            return _get_response_data(request)


@pytest.fixture(params=["is_async", "is_sync"])
def _post_endpoint(request, test_path: str, router: VersionedAPIRouter, latest_module: ModuleType):
    def _get_request_data(request: Request, body: latest_module.AnyRequestSchema):
        if not PYDANTIC_V2:
            body = body.__root__
        return {
            "body": body,
            "headers": dict(request.headers),
            "cookies": request.cookies,
            "query_params": dict(request.query_params),
        }

    if request.param == "is_async":

        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def post_async_endpoint(request: Request, body: latest_module.AnyRequestSchema):
            return _get_request_data(request, body)

    else:

        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        def post_sync_endpoint(request: Request, body: latest_module.AnyRequestSchema):
            return _get_request_data(request, body)


@pytest.fixture(params=["by path", "by schema"])
def version_change_1(request, test_path: str, latest_module):
    if request.param == "by path":
        convert_request = convert_request_to_next_version_for(test_path, ["POST"])
        convert_response = convert_response_to_previous_version_for(test_path, ["POST"])
    else:
        convert_request = convert_request_to_next_version_for(latest_module.AnyRequestSchema)
        convert_response = convert_response_to_previous_version_for(latest_module.AnyResponseSchema)

    @convert_request
    def change_address_to_multiple_items(request: RequestInfo):
        request.body.append("request change 1")

    @convert_response
    def change_addresses_to_single_item(response: ResponseInfo) -> None:
        response.body["body"].append("response change 1")

    return version_change(
        change_address_to_multiple_items=change_address_to_multiple_items,
        change_addresses_to_single_item=change_addresses_to_single_item,
    )


@pytest.fixture()
def version_change_2(latest_module):
    @convert_request_to_next_version_for(latest_module.AnyRequestSchema)
    def change_addresses_to_default_address(request: RequestInfo):
        request.body.append("request change 2")

    @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
    def change_addresses_to_list(response: ResponseInfo) -> None:
        response.body["body"].append("response change 2")

    return version_change(
        change_addresses_to_default_address=change_addresses_to_default_address,
        change_addresses_to_list=change_addresses_to_list,
    )


@pytest.fixture(params=["no request", "with request"])
def _post_endpoint_with_extra_depends(  # noqa: PT005
    request: pytest.FixtureRequest,
    router: VersionedAPIRouter,
    test_path: Literal["/test"],
    latest_module: ModuleType,
    _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],  # pyright: ignore[reportGeneralTypeIssues]
):
    if request.param == "no request":
        router.routes = []

        @router.post(test_path)
        async def _post_endpoint(
            body: latest_module.AnyRequestSchema,
            header_key: str | None = Header(default=None, alias="header_key"),
            n_header: str | None = Header(default=None, alias="3"),
            cookie_key: str | None = Cookie(default=None),
            n_cookie: str | None = Cookie(default=None, alias="5"),
            query_param_key: str | None = Query(default=None),
            n_query: str | None = Query(default=None, alias="7"),
        ):
            headers: Any = {"header_key": header_key}
            cookies: Any = {"cookie_key": cookie_key}
            query_params: Any = {"query_param_key": query_param_key}

            if n_header is not None:
                headers["3"] = n_header
                cookies["5"] = n_cookie
                query_params["7"] = n_query
            if not PYDANTIC_V2:
                body = body.__root__
            return {
                "body": body,
                "headers": dict(headers),
                "cookies": cookies,
                "query_params": dict(query_params),
            }

    return _post_endpoint


class TestRequestMigrations:
    def test__all_request_components_migration__post_endpoint__migration_filled_results_up(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        test_path: Literal["/test"],
        _post_endpoint_with_extra_depends: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    ):
        @convert_request_to_next_version_for(latest_module.AnyRequestSchema)
        def migrator(request: RequestInfo):
            request.body["hello"] = "hello"
            request.headers["header_key"] = "header val 2"
            request.cookies["cookie_key"] = "cookie val 2"
            request.query_params["query_param_key"] = "query_param val 2"

        clients = create_versioned_clients(version_change(migrator=migrator))

        assert clients[date(2000, 1, 1)].post(test_path, json={}).json() == {
            "body": {"hello": "hello"},
            "headers": IsPartialDict({"header_key": "header val 2"}),
            "cookies": {"cookie_key": "cookie val 2"},
            "query_params": {"query_param_key": "query_param val 2"},
        }

        clients[date(2000, 1, 1)].cookies["5"] = "6"
        assert clients[date(2000, 1, 1)].post(
            test_path,
            json={"1": "2"},
            headers={"3": "4"},
            params={"7": "8"},
        ).json() == {
            "body": {"1": "2", "hello": "hello"},
            "headers": IsPartialDict({"header_key": "header val 2", "3": "4"}),
            "cookies": {"cookie_key": "cookie val 2", "5": "6"},
            "query_params": {"query_param_key": "query_param val 2", "7": "8"},
        }

    def test__all_request_components_migration__get_endpoint__migration_filled_results_up(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.get(test_path)
        async def get(request: Request):
            return {
                "body": await request.body(),
                "headers": dict(request.headers),
                "cookies": request.cookies,
                "query_params": dict(request.query_params),
            }

        @convert_request_to_next_version_for(test_path, ["GET"])
        def migrator(request: RequestInfo):
            request.headers["request2"] = "request2"
            request.cookies["request2"] = "request2"
            request.query_params["request2"] = "request2"

        clients = create_versioned_clients(version_change(migrator=migrator))
        assert clients[date(2000, 1, 1)].get(test_path).json() == {
            "body": "",
            "headers": IsPartialDict({"request2": "request2"}),
            "cookies": {"request2": "request2"},
            "query_params": {"request2": "request2"},
        }

    def test__depends_gets_broken_after_migration__should_raise_422(
        self,
        create_versioned_clients: CreateVersionedClients,
        router: VersionedAPIRouter,
        test_path,
    ):
        @router.get(test_path)
        async def get(my_header: str = Header()):
            return 83

        @convert_request_to_next_version_for(test_path, ["GET"])
        def migrator(request: RequestInfo):
            del request.headers["my-header"]

        clients = create_versioned_clients(version_change(migrator=migrator))

        response = clients[date(2000, 1, 1)].get(test_path, headers={"my-header": "wow"}).json()
        if PYDANTIC_V2:
            assert response == {
                "detail": [
                    {
                        "type": "missing",
                        "loc": ["header", "my-header"],
                        "msg": "Field required",
                        "input": None,
                        "url": "https://errors.pydantic.dev/2.5/v/missing",
                    },
                ],
            }
        else:
            assert response == {
                "detail": [
                    {
                        "loc": ["header", "my-header"],
                        "msg": "field required",
                        "type": "value_error.missing",
                    },
                ],
            }
        assert clients[date(2001, 1, 1)].get(test_path, headers={"my-header": "wow"}).json() == 83

    def test__optional_body_field(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(payload: latest_module.AnyRequestSchema | None = Body(None)):
            return payload or {"hello": "world"}

        @convert_request_to_next_version_for(latest_module.AnyRequestSchema)
        def migrator(request: RequestInfo):
            assert request.body is None

        clients = create_versioned_clients(version_change(migrator=migrator))

        assert clients[date(2000, 1, 1)].post(test_path).json() == {"hello": "world"}
        assert clients[date(2001, 1, 1)].post(test_path).json() == {"hello": "world"}

    def test__internal_schema_specified__with_no_migrations__body_gets_parsed_to_internal_request_schema(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        temp_data_package_path: str,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(
            payload: Annotated[
                latest_module.InternalSchema,
                InternalRepresentationOf[latest_module.SchemaWithInternalRepresentation],
                str,
            ],
        ):
            return {"type": type(payload).__name__, **model_dump(payload)}

        clients = create_versioned_clients(version_change())

        last_route = clients[date(2000, 1, 1)].app.routes[-1]
        assert isinstance(last_route, APIRoute)
        payload_arg = last_route.endpoint.__annotations__["payload"]
        assert get_args(payload_arg)[1] == str

        assert clients[date(2000, 1, 1)].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "type": "InternalSchema",
            "foo": 1,
            # we expect for the passed "bar" attribute to not get passed because it's not in the public schema
            "bar": None,
        }
        assert clients[date(2001, 1, 1)].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "type": "InternalSchema",
            "foo": 1,
            "bar": None,
        }

    def test__internal_schema_specified__with_migrations__body_gets_parsed_to_internal_request_schema(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        temp_data_package_path: str,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(
            payload: Annotated[
                latest_module.InternalSchema,
                InternalRepresentationOf[latest_module.SchemaWithInternalRepresentation],
            ],
        ):
            return {"type": type(payload).__name__, **model_dump(payload)}

        @convert_request_to_next_version_for(latest_module.SchemaWithInternalRepresentation)
        def migrator(request: RequestInfo):
            request.body["bar"] = "world"

        clients = create_versioned_clients(version_change(migrator=migrator))

        assert clients[date(2000, 1, 1)].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "type": "InternalSchema",
            "foo": 1,
            "bar": "world",
        }
        assert clients[date(2001, 1, 1)].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "type": "InternalSchema",
            "foo": 1,
            "bar": None,
        }

    def test__internal_schema_specified__with_invalid_migrations__internal_schema_validation_error(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        temp_data_package_path: str,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(
            payload: Annotated[
                latest_module.InternalSchema,
                InternalRepresentationOf[latest_module.SchemaWithInternalRepresentation],
            ],
        ):
            return {"type": type(payload).__name__, **model_dump(payload)}

        @convert_request_to_next_version_for(latest_module.SchemaWithInternalRepresentation)
        def migrator(request: RequestInfo):
            request.body["bar"] = [1, 2, 3]

        clients = create_versioned_clients(version_change(migrator=migrator))
        response = clients[date(2000, 1, 1)].post(test_path, json={"foo": 1, "bar": "hewwo"}).json()
        if PYDANTIC_V2:
            assert response == {
                "detail": [
                    {
                        "input": [1, 2, 3],
                        "loc": ["body", "bar"],
                        "msg": "Input should be a valid string",
                        "type": "string_type",
                        "url": "https://errors.pydantic.dev/2.5/v/string_type",
                    },
                ],
            }
        else:
            assert response == {
                "detail": [{"loc": ["body", "bar"], "msg": "str type expected", "type": "type_error.str"}],
            }
        assert clients[date(2001, 1, 1)].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "type": "InternalSchema",
            "foo": 1,
            "bar": None,
        }


class TestResponseMigrations:
    def test__all_response_components_migration__post_endpoint__migration_filled_results_up(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        test_path: Literal["/test"],
        _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    ):
        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.body["body_key"] = "body_val"
            assert response.status_code is None
            response.status_code = 300
            response.headers["header"] = "header_val"
            response.set_cookie("cookie_key", "cookie_val", max_age=83)

        clients = create_versioned_clients(version_change(migrator=migrator))

        resp = clients[date(2000, 1, 1)].post(test_path, json={})

        assert resp.json() == {
            "body": {},
            "headers": {
                "host": "testserver",
                "accept": "*/*",
                "accept-encoding": "gzip, deflate",
                "connection": "keep-alive",
                "user-agent": "testclient",
                "content-length": "2",
                "content-type": "application/json",
                "x-api-version": "2000-01-01",
            },
            "cookies": {},
            "query_params": {},
            "body_key": "body_val",
        }
        assert dict(resp.headers) == {
            "content-length": "281",
            "content-type": "application/json",
            "header": "header_val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
            "x-api-version": "2000-01-01",
        }
        assert dict(resp.cookies) == {"cookie_key": "cookie_val"}
        assert resp.status_code == 300

        clients[date(2000, 1, 1)].cookies["5"] = "6"
        resp = clients[date(2000, 1, 1)].post(test_path, json={"1": "2"}, headers={"3": "4"})
        assert resp.json() == {
            "body": {"1": "2"},
            "headers": {
                "host": "testserver",
                "accept": "*/*",
                "accept-encoding": "gzip, deflate",
                "connection": "keep-alive",
                "user-agent": "testclient",
                "3": "4",
                "cookie": IsStr(min_length=3),
                "content-length": "10",
                "content-type": "application/json",
                "x-api-version": "2000-01-01",
            },
            "cookies": {"5": "6", "cookie_key": "cookie_val"},
            "query_params": {},
            "body_key": "body_val",
        }
        assert dict(resp.headers) == {
            "content-length": "368",
            "content-type": "application/json",
            "header": "header_val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
            "x-api-version": "2000-01-01",
        }
        assert dict(resp.cookies) == {"cookie_key": "cookie_val"}
        assert resp.status_code == 300

    def test__all_response_components_migration__get_endpoint__migration_filled_results_up(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module: ModuleType,
        _get_endpoint,
    ):
        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.status_code = 300
            response.headers["header_key"] = "header-val"
            response.set_cookie("cookie_key", "cookie_val", max_age=83)

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients[date(2000, 1, 1)].get(test_path)
        assert dict(resp.headers) == {
            "content-length": "194",
            "content-type": "application/json",
            "header_key": "header-val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
            "x-api-version": "2000-01-01",
        }
        assert dict(resp.cookies) == {"cookie_key": "cookie_val"}
        assert resp.status_code == 300

    def test__fastapi_response_migration__post_endpoint(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module: ModuleType,
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def post_endpoint(request: Request):
            return JSONResponse({"hewwo": "darkness"}, status_code=301, headers={"header-key": "header-val"})

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migrator(response: ResponseInfo):
            assert response.status_code == 301
            assert response.headers["header-key"] == "header-val"
            response.body |= {"migration": "body"}
            response.status_code = 201
            response.headers["header-key"] = "header-val2"

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients[date(2000, 1, 1)].post(test_path, json={})
        assert resp.json() == {"hewwo": "darkness", "migration": "body"}
        assert dict(resp.headers) == (
            {
                "header-key": "header-val2",
                "content-length": "20",
                "content-type": "application/json",
                "x-api-version": "2000-01-01",
            }
        )
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients[date(2001, 1, 1)].post(test_path, json={})
        assert resp.json() == {"hewwo": "darkness"}
        assert dict(resp.headers) == (
            {
                "header-key": "header-val",
                "content-length": "20",
                "content-type": "application/json",
                "x-api-version": "2001-01-01",
            }
        )
        assert resp.status_code == 301

    def test__fastapi_response_migration__response_only_has_status_code_and_there_is_a_migration(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module: ModuleType,
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def post_endpoint(request: Request):
            return Response(status_code=200)

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.status_code = 201

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients[date(2000, 1, 1)].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2000-01-01",
            }
        )
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients[date(2001, 1, 1)].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2001-01-01",
            }
        )
        assert resp.status_code == 200

    def test__fastapi_response_migration__response_is_streaming_response_and_there_is_a_migration(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module: ModuleType,
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def post_endpoint(request: Request):
            return StreamingResponse(StringIO("streaming response"), status_code=200)

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.status_code = 201

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients[date(2000, 1, 1)].post(test_path, json={})
        assert resp.content == b"streaming response"
        assert dict(resp.headers) == {"x-api-version": "2000-01-01"}
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients[date(2001, 1, 1)].post(test_path, json={})
        assert resp.content == b"streaming response"
        assert dict(resp.headers) == {"x-api-version": "2001-01-01"}
        assert resp.status_code == 200

    def test__fastapi_response_migration__response_only_has_status_code_and_there_is_no_migration(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module: ModuleType,
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def post_endpoint(request: Request):
            return Response(status_code=200)

        clients = create_versioned_clients(version_change())
        resp = clients[date(2000, 1, 1)].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2000-01-01",
            }
        )
        assert resp.status_code == 200
        assert dict(resp.cookies) == {}

        resp = clients[date(2001, 1, 1)].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2001-01-01",
            }
        )
        assert resp.status_code == 200


class TestHowAndWhenMigrationsApply:
    def test__migrate_request_and_response__with_no_migrations__should_not_raise_error(
        self,
        test_path: Literal["/test"],
        create_versioned_clients: CreateVersionedClients,
        _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    ):
        clients = create_versioned_clients()
        assert clients[date(2000, 1, 1)].post(test_path, json={"A": "B"}).json() == {
            "body": {"A": "B"},
            "headers": IsPartialDict(),
            "cookies": {},
            "query_params": {},
        }

    def test__migrate_request__with_no_migrations__request_schema_should_be_from_latest(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module,
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def endpoint(foo: latest_module.AnyRequestSchema):
            assert isinstance(
                foo, latest_module.AnyRequestSchema
            ), f"Request schema is from: {foo.__class__.__module__}"
            return {}

        clients = create_versioned_clients(version_change(), version_change())
        resp_2000 = clients[date(2000, 1, 1)].post(test_path, json={})
        assert resp_2000.status_code, resp_2000.json()

        resp_2001 = clients[date(2001, 1, 1)].post(test_path, json={})
        assert resp_2001.status_code, resp_2001.json()

        resp_2002 = clients[date(2002, 1, 1)].post(test_path, json={})
        assert resp_2002.status_code, resp_2002.json()

    def test__migrate_one_version_down__migrations_are_applied_to_2000_version_but_not_to_2000(
        self,
        version_change_1: type[VersionChange],
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1)
        assert clients[date(2000, 1, 1)].post(test_path, json=[]).json()["body"] == [
            "request change 1",
            "response change 1",
        ]
        assert clients[date(2001, 1, 1)].post(test_path, json=[]).json()["body"] == []

    def test__migrate_two_versions_down__2002_applies_to_2001_and_2000_while_2001_only_applies_to_2000(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        assert clients[date(2000, 1, 1)].post(test_path, json=[]).json()["body"] == [
            "request change 1",
            "request change 2",
            "response change 2",
            "response change 1",
        ]
        assert clients[date(2001, 1, 1)].post(test_path, json=[]).json()["body"] == [
            "request change 2",
            "response change 2",
        ]
        assert clients[date(2002, 1, 1)].post(test_path, json=[]).json()["body"] == []

    def test__try_migrating_when_version_is_none__no_migrations_get_applied(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        api_version_var: ContextVar[date | None],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        app = clients[date(2000, 1, 1)].app
        none_client = client(
            APIRouter(routes=app.router.versioned_routes[date(2000, 1, 1)]),
            api_version=None,
            api_version_var=api_version_var,
        )
        # The version below is not actually used anywhere, but it's required so we pass a dummy one
        assert (
            none_client.post(
                test_path,
                json=[],
                headers={app.router.api_version_header_name: "2000-11-11"},
            ).json()["body"]
            == []
        )

    def test__try_migrating_to_version_below_earliest__undefined_behaior(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        api_version_var: ContextVar[date | None],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        app = clients[date(2000, 1, 1)].app
        earlier_client = client(
            APIRouter(routes=app.router.versioned_routes[date(2000, 1, 1)]),
            api_version=date(1998, 2, 10),
            api_version_var=api_version_var,
        )
        assert earlier_client.post(
            test_path,
            json=[],
            headers={app.router.api_version_header_name: "2000-01-01"},
        ).json()["body"] == [
            "request change 1",
            "request change 2",
            "response change 2",
            "response change 1",
        ]

    def test__try_migrating_to_version_above_latest__no_migrations_get_applied(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        api_version_var: ContextVar[date | None],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        app = clients[date(2000, 1, 1)].app
        assert (
            clients[date(2000, 1, 1)]
            .post(test_path, json=[], headers={app.router.api_version_header_name: "2050-01-01"})
            .json()["body"]
            == []
        )

    def test__migrate_one_version_down__with_inapplicable_migrations__result_is_only_affected_by_applicable_migrations(
        self,
        version_change_1: type[VersionChange],
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        _post_endpoint,
        latest_module,
    ):
        def bad_req(request: RequestInfo):
            raise NotImplementedError("I was not supposed to be ever called! This is very bad!")

        def bad_resp(response: ResponseInfo):
            raise NotImplementedError("I was not supposed to be ever called! This is very bad!")

        clients = create_versioned_clients(
            [
                version_change_1,
                version_change(
                    wrong_body_schema=convert_request_to_next_version_for(latest_module.AnyResponseSchema)(bad_req),
                    wrong_resp_schema=convert_response_to_previous_version_for(latest_module.AnyRequestSchema)(
                        bad_resp,
                    ),
                    wrong_req_path=convert_request_to_next_version_for("/wrong_path", ["POST"])(bad_req),
                    wrong_req_method=convert_request_to_next_version_for(test_path, ["GET"])(bad_req),
                    wrong_resp_path=convert_response_to_previous_version_for("/wrong_path", ["POST"])(bad_resp),
                    wrong_resp_method=convert_response_to_previous_version_for(test_path, ["GET"])(bad_resp),
                ),
            ],
        )
        assert len(clients) == 2
        assert clients[date(2000, 1, 1)].post(test_path, json=[]).json()["body"] == [
            "request change 1",
            "response change 1",
        ]
        assert clients[date(2001, 1, 1)].post(test_path, json=[]).json()["body"] == []

    def test__cookies_can_be_deleted_during_migrations(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        latest_module,
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=latest_module.AnyResponseSchema)
        async def endpoint(response: Response):
            response.set_cookie("cookie_key", "cookie_val")
            return 83

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migration(response: ResponseInfo):
            response.delete_cookie("cookie_key")

        clients = create_versioned_clients(version_change(migration=migration))
        resp_2000 = clients[date(2000, 1, 1)].post(test_path, json={})
        resp_2001 = clients[date(2001, 1, 1)].post(test_path, json={})

        assert dict(resp_2000.cookies) == {"cookie_key": "cookie_val"}

        assert dict(resp_2000.headers) == {
            "content-length": "2",
            "content-type": "application/json",
            "set-cookie": IsStr(),
            "x-api-version": "2000-01-01",
        }
        assert dict(http.cookies.SimpleCookie(resp_2000.headers["set-cookie"])["cookie_key"]) == {
            "expires": IsStr(),
            "path": "/",
            "comment": "",
            "domain": "",
            "max-age": "0",
            "secure": "",
            "httponly": "",
            "version": "",
            "samesite": "lax",
        }
        assert dict(resp_2001.cookies) == {"cookie_key": "cookie_val"}
        assert dict(resp_2001.headers) == {
            "content-length": "2",
            "content-type": "application/json",
            "set-cookie": "cookie_key=cookie_val; Path=/; SameSite=lax",
            "x-api-version": "2001-01-01",
        }


def test__invalid_path_migration_syntax():
    with pytest.raises(
        ValueError,
        match=re.escape("If path was provided as a first argument, methods must be provided as a second argument"),
    ):
        convert_request_to_next_version_for("/test")  # pyright: ignore[reportGeneralTypeIssues]


def test__invalid_schema_migration_syntax(latest_module):
    with pytest.raises(
        ValueError,
        match=re.escape("If schema was provided as a first argument, methods argument should not be provided"),
    ):
        convert_request_to_next_version_for(latest_module.AnyRequestSchema, ["POST"])


def test__defining_two_migrations_for_the_same_request(latest_module):
    with pytest.raises(
        CadwynStructureError,
        match=re.escape('There already exists a request migration for "AnyRequestSchema" in "MyVersionChange".'),
    ):

        @convert_request_to_next_version_for(latest_module.AnyRequestSchema)
        def migration1(request: RequestInfo):
            raise NotImplementedError

        @convert_request_to_next_version_for(latest_module.AnyRequestSchema)
        def migration2(request: RequestInfo):
            raise NotImplementedError

        version_change(migration1=migration1, migration2=migration2)


def test__defining_two_migrations_for_the_same_response(latest_module):
    with pytest.raises(
        CadwynStructureError,
        match=re.escape('There already exists a response migration for "AnyResponseSchema" in "MyVersionChange".'),
    ):

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migration1(response: ResponseInfo):
            raise NotImplementedError

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migration2(response: ResponseInfo):
            raise NotImplementedError

        version_change(migration1=migration1, migration2=migration2)


def test__uploadfile_can_work(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    latest_module,
    router: VersionedAPIRouter,
):
    @router.post(test_path, response_model=latest_module.AnyResponseSchema)
    async def endpoint(file: UploadFile = File(...)):
        # PydanticV2 can no longer serialize files directly like it could in v1
        file_dict = {k: v for k, v in file.__dict__.items() if not k.startswith("_") and k != "file"}
        file_dict["headers"] = dict(file_dict["headers"])
        return file_dict

    clients = create_versioned_clients(version_change())
    resp_2000 = clients[date(2000, 1, 1)].post(test_path, files={"file": b"Hewwo"})
    resp_2001 = clients[date(2001, 1, 1)].post(test_path, files={"file": b"Hewwo"})

    assert resp_2000.json() == {
        "filename": "upload",
        "size": 5,
        "headers": {
            "content-disposition": 'form-data; name="file"; filename="upload"',
            "content-type": "application/octet-stream",
        },
    }
    assert resp_2001.json() == {
        "filename": "upload",
        "size": 5,
        "headers": {
            "content-disposition": 'form-data; name="file"; filename="upload"',
            "content-type": "application/octet-stream",
        },
    }
