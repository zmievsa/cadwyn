import dataclasses
import http.cookies
import re
import sys
import tempfile
import typing
from collections.abc import Callable, Coroutine, Iterator
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal
from enum import Enum
from io import BytesIO, RawIOBase, StringIO, UnsupportedOperation
from typing import Annotated, Any, Literal, Optional, Union, cast
from uuid import UUID

import fastapi
import pytest
from dirty_equals import IsPartialDict, IsStr
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
)
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient
from pydantic import (
    AfterValidator,
    AliasChoices,
    AliasGenerator,
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Json,
    OnErrorOmit,
    PlainSerializer,
    PlainValidator,
    RootModel,
    Strict,
    ValidationError,
    WrapSerializer,
    WrapValidator,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
    root_validator,  # ty: ignore[deprecated]  # V1 validators are still supported.
    validator,  # ty: ignore[deprecated]  # V1 validators are still supported.
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic_core import core_schema
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartParser
from starlette.responses import StreamingResponse
from typing_extensions import Self, TypeAliasType, TypedDict, override

from cadwyn import VersionedAPIRouter, current_dependency_solver
from cadwyn.exceptions import (
    CadwynError,
    CadwynHeadRequestValidationError,
    RouteByPathConverterDoesNotApplyToAnythingError,
    RouteRequestBySchemaConverterDoesNotApplyToAnythingError,
    RouteResponseBySchemaConverterDoesNotApplyToAnythingError,
)
from cadwyn.schema_generation import migrate_response_body
from cadwyn.structure import (
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
)
from cadwyn.structure.data import RequestInfo, ResponseInfo
from cadwyn.structure.endpoints import endpoint as cadwyn_endpoint
from cadwyn.structure.schemas import schema
from cadwyn.structure.versions import Version, VersionBundle
from tests._resources.utils import (
    FASTAPI_DISTINGUISHES_NULL_BODIES,
    FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES,
    FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS,
    FASTAPI_VALIDATES_FORM_FIELDS_OF_OVERRIDES,
)
from tests.conftest import (
    CreateVersionedApp,
    CreateVersionedClients,
    client,
    version_change,
)


@pytest.fixture
def test_path():
    return "/test"


class EmptySchema(BaseModel):
    pass


# so `RootModel[Any] is RootModel[Any]`. This causes dire consequences if you try to make "different"
# request and response root models with the same definitions
class AnyRequestSchema(RootModel[Any]):
    pass


class AnyResponseSchema(RootModel[Any]):
    pass


class SchemaWithHeadMigrations(BaseModel):
    foo: int
    bar: Union[str, None] = Field(default=None)


class SchemaWithInternalRepresentation(BaseModel):
    foo: int


class InternalSchema(SchemaWithInternalRepresentation):
    bar: Union[str, None] = Field(default=None)


class TypedDictResponse(TypedDict):
    foo: int


@pytest.fixture(params=["is_async", "is_sync"])
def _get_endpoint(
    request: pytest.FixtureRequest,
    test_path: str,
    router: VersionedAPIRouter,
):
    def _get_response_data(request: Request):
        return {
            "headers": dict(request.headers),
            "cookies": request.cookies,
            "query_params": dict(request.query_params),
        }

    if request.param == "is_async":

        @router.get(test_path, response_model=AnyResponseSchema)
        async def get_async_endpoint(request: Request):
            return _get_response_data(request)

    else:

        @router.get(test_path, response_model=AnyResponseSchema)
        def get_sync_endpoint(request: Request):
            return _get_response_data(request)


@pytest.fixture(params=["is_async", "is_sync"])
def _post_endpoint(request, test_path: str, router: VersionedAPIRouter):
    def _get_request_data(request: Request, body: AnyRequestSchema):
        return {
            "body": body,
            "headers": dict(request.headers),
            "cookies": request.cookies,
            "query_params": dict(request.query_params),
        }

    if request.param == "is_async":

        @router.post(test_path, response_model=AnyResponseSchema)
        async def post_async_endpoint(request: Request, body: AnyRequestSchema):
            return _get_request_data(request, body)

    else:

        @router.post(test_path, response_model=AnyResponseSchema)
        def post_sync_endpoint(request: Request, body: AnyRequestSchema):
            return _get_request_data(request, body)


@pytest.fixture(params=["by path", "by schema"])
def version_change_1(
    request,
    test_path: str,
):
    if request.param == "by path":
        convert_request = convert_request_to_next_version_for(test_path, ["POST"])
        convert_response = convert_response_to_previous_version_for(test_path, ["POST"])
    else:
        convert_request = convert_request_to_next_version_for(AnyRequestSchema)
        convert_response = convert_response_to_previous_version_for(AnyResponseSchema)

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


@pytest.fixture
def version_change_2():
    @convert_request_to_next_version_for(AnyRequestSchema)
    def change_addresses_to_default_address(request: RequestInfo):
        request.body.append("request change 2")

    @convert_response_to_previous_version_for(AnyResponseSchema)
    def change_addresses_to_list(response: ResponseInfo) -> None:
        response.body["body"].append("response change 2")

    return version_change(
        change_addresses_to_default_address=change_addresses_to_default_address,
        change_addresses_to_list=change_addresses_to_list,
    )


@pytest.fixture(params=["without_request", "with request"])
def _post_endpoint_with_extra_depends(
    request: pytest.FixtureRequest,
    router: VersionedAPIRouter,
    test_path: Literal["/test"],
    _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
):
    if request.param == "without_request":
        router.routes = []

        @router.post(test_path)
        async def _post_endpoint(
            body: AnyRequestSchema,
            header_key: Union[str, None] = Header(default=None, alias="header_key"),
            n_header: Union[str, None] = Header(default=None, alias="3"),
            cookie_key: Union[str, None] = Cookie(default=None),
            n_cookie: Union[str, None] = Cookie(default=None, alias="5"),
            query_param_key: Union[str, None] = Query(default=None),
            n_query: Union[str, None] = Query(default=None, alias="7"),
        ):
            headers: Any = {"header_key": header_key}
            cookies: Any = {"cookie_key": cookie_key}
            query_params: Any = {"query_param_key": query_param_key}

            if n_header is not None:
                headers["3"] = n_header
                cookies["5"] = n_cookie
                query_params["7"] = n_query
            return {
                "body": body,
                "headers": dict(headers),
                "cookies": cookies,
                "query_params": dict(query_params),
            }


class TestRequestMigrations:
    def test__all_request_components_migration__post_endpoint__migration_filled_results_up(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        _post_endpoint_with_extra_depends: None,
    ):
        @convert_request_to_next_version_for(AnyRequestSchema)
        def migrator(request: RequestInfo):
            request.body["hello"] = "hello"
            request.headers["header_key"] = "header val 2"
            request.cookies["cookie_key"] = "cookie val 2"
            request.query_params["query_param_key"] = "query_param val 2"

        clients = create_versioned_clients(version_change(migrator=migrator))

        assert clients["2000-01-01"].post(test_path, json={}).json() == {
            "body": {"hello": "hello"},
            "headers": IsPartialDict({"header_key": "header val 2"}),
            "cookies": {"cookie_key": "cookie val 2"},
            "query_params": {"query_param_key": "query_param val 2"},
        }

        clients["2000-01-01"].cookies["5"] = "6"
        assert clients["2000-01-01"].post(
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
        assert clients["2000-01-01"].get(test_path).json() == {
            "body": "",
            "headers": IsPartialDict({"request2": "request2"}),
            "cookies": {"request2": "request2"},
            "query_params": {"request2": "request2"},
        }

    def test__depends_gets_broken_after_migration__should_raise_500(
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

        assert clients["2001-01-01"].get(test_path, headers={"my-header": "wow"}).json() == 83
        with pytest.raises(CadwynHeadRequestValidationError):
            clients["2000-01-01"].get(test_path, headers={"my-header": "wow"}).json()

    def test__head_schema_migration__with_no_versioned_migrations__body_gets_parsed_to_head_schema(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(payload: SchemaWithHeadMigrations):
            return payload

        clients = create_versioned_clients(
            version_change(),
            head_version_changes=[version_change(schema(SchemaWithHeadMigrations).field("bar").didnt_exist)],
        )

        # [-1] route is /openapi.json
        last_route = clients["2000-01-01"].app.router.versioned_routers["2000-01-01"].routes[-1]
        assert isinstance(last_route, APIRoute)

        assert clients["2000-01-01"].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "foo": 1,
            "bar": None,
        }
        assert clients["2001-01-01"].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "foo": 1,
            "bar": None,
        }

    def test__head_schema_migration__with_versioned_migrations__body_gets_parsed_to_internal_request_schema(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(payload: SchemaWithHeadMigrations):
            return payload

        @convert_request_to_next_version_for(SchemaWithHeadMigrations)
        def migrator(request: RequestInfo):
            request.body["bar"] = "world"

        clients = create_versioned_clients(
            version_change(migrator=migrator),
            head_version_changes=[version_change(schema(SchemaWithHeadMigrations).field("bar").didnt_exist)],
        )

        assert clients["2000-01-01"].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "foo": 1,
            "bar": "world",
        }
        assert clients["2001-01-01"].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "foo": 1,
            "bar": None,
        }

    def test__head_schema_migration__with_invalid_versioned_migrations__internal_schema_validation_error(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(payload: SchemaWithHeadMigrations):
            return payload

        @convert_request_to_next_version_for(SchemaWithHeadMigrations)
        def migrator(request: RequestInfo):
            request.body["bar"] = [1, 2, 3]

        clients = create_versioned_clients(
            version_change(migrator=migrator),
            head_version_changes=[version_change(schema(SchemaWithHeadMigrations).field("bar").didnt_exist)],
        )
        assert clients["2001-01-01"].post(test_path, json={"foo": 1, "bar": "hewwo"}).json() == {
            "foo": 1,
            "bar": None,
        }
        with pytest.raises(CadwynHeadRequestValidationError):
            clients["2000-01-01"].post(test_path, json={"foo": 1, "bar": "hewwo"}).json()

    def test__serialization_of_request_body__when_body_is_non_pydantic(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def route(payload: dict[str, int] = Body(None)):
            return payload

        @convert_request_to_next_version_for(test_path, ["POST"])
        def migrator(request: RequestInfo):
            assert request.body == {"foo": 1}
            request.body["foo"] += 1

        payload = {"foo": "1"}
        clients = create_versioned_clients(version_change(migrator=migrator))
        assert clients["2000-01-01"].post(url=test_path, json=payload).json() == {"foo": 2}
        assert clients["2001-01-01"].post(url=test_path, json=payload).json() == {"foo": 1}
        empty_response = clients["2001-01-01"].post(url=test_path)
        assert empty_response.status_code == 200
        assert empty_response.json() is None

    @pytest.mark.parametrize("body_type", [SchemaWithInternalRepresentation, dict[str, int]])
    def test__request_migration__setting_body_to_none__should_omit_the_body(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
        body_type: Any,
    ):
        @router.post(test_path)
        async def route(payload: body_type = Body(None)):
            return payload

        @convert_request_to_next_version_for(test_path, ["POST"])
        def migrator(request: RequestInfo):
            request.body = None

        clients = create_versioned_clients(version_change(migrator=migrator))
        response = clients["2000-01-01"].post(url=test_path, json={"foo": 1})
        assert response.status_code == 200
        assert response.json() is None
        assert clients["2001-01-01"].post(url=test_path, json={"foo": 1}).json() == {"foo": 1}

    @pytest.mark.parametrize("content", [b'{"foo": 1}', b"null"])
    @pytest.mark.parametrize("fills_body_before_clearing_it", [False, True])
    def test__request_migration__clearing_body__should_omit_it_so_that_the_default_applies(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
        content: bytes,
        fills_body_before_clearing_it: bool,
    ):
        @router.post(test_path)
        async def route(payload: Optional[dict[str, int]] = Body({"default": 1})):
            return payload

        @convert_request_to_next_version_for(test_path, ["POST"])
        def fill(request: RequestInfo):
            request.body = {"temporary": 1}

        @convert_request_to_next_version_for(test_path, ["POST"])
        def clear(request: RequestInfo):
            request.body = None

        # Even a JSON null body is omitted once a migration has set the body to None
        changes = [version_change(fill=fill)] if fills_body_before_clearing_it else []
        clients = create_versioned_clients(*changes, version_change(clear=clear))
        response = clients["2000-01-01"].post(
            url=test_path, content=content, headers={"content-type": "application/json"}
        )
        assert (response.status_code, response.json()) == (200, {"default": 1})

    @pytest.mark.parametrize("content", [b'{"foo": 1}', b"null"])
    def test__request_migration__clearing_body_for_a_body_dependency_of_newer_versions__should_omit_it(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
        content: bytes,
    ):
        def added_body_dependency(extra: Optional[dict[str, int]] = Body(None)):
            return extra

        @router.post(test_path, dependencies=[Depends(added_body_dependency)])
        async def route(payload: Optional[dict[str, int]] = Body(None)):
            return payload

        @convert_request_to_next_version_for(test_path, ["POST"])
        def clear(request: RequestInfo):
            request.body = None

        clients = create_versioned_clients(
            version_change(cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[]), clear=clear)
        )
        response = clients["2000-01-01"].post(
            url=test_path, content=content, headers={"content-type": "application/json"}
        )
        assert (response.status_code, response.json()) == (200, None)

    def test__request_migration__when_body_annotation_is_unhashable__should_migrate_by_path(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        # Dict metadata inside Annotated makes the whole annotation unhashable
        @router.post(test_path)
        async def route(payload: list[Annotated[int, {"description": "item"}]]):
            return payload

        @convert_request_to_next_version_for(test_path, ["POST"])
        def migrator(request: RequestInfo):
            request.body.append(3)

        clients = create_versioned_clients(version_change(migrator=migrator))
        assert clients["2000-01-01"].post(url=test_path, json=[1, 2]).json() == [1, 2, 3]
        assert clients["2001-01-01"].post(url=test_path, json=[1, 2]).json() == [1, 2]


class TestResponseMigrations:
    def test__all_response_components_migration__post_endpoint__migration_filled_results_up(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    ):
        @convert_response_to_previous_version_for(AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.body["body_key"] = "body_val"
            assert response.status_code == 200
            response.status_code = 300
            response.headers["header"] = "header_val"
            response.set_cookie("cookie_key", "cookie_val", max_age=83)

        clients = create_versioned_clients(version_change(migrator=migrator))

        resp = clients["2000-01-01"].post(test_path, json={})

        assert resp.json() == {
            "body": {},
            "headers": {
                "host": "testserver",
                "accept": "*/*",
                # 3.14+ also sends zstd
                "accept-encoding": IsStr(regex=r"^gzip, deflate(, zstd)?$"),
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
            "content-length": str(len(resp.content)),
            "content-type": "application/json",
            "header": "header_val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
            "x-api-version": "2000-01-01",
        }
        assert dict(resp.cookies) == {"cookie_key": "cookie_val"}
        assert resp.status_code == 300

        clients["2000-01-01"].cookies["5"] = "6"
        resp = clients["2000-01-01"].post(test_path, json={"1": "2"}, headers={"3": "4"})
        assert resp.json() == {
            "body": {"1": "2"},
            "headers": {
                "host": "testserver",
                "accept": "*/*",
                "accept-encoding": IsStr(regex=r"^gzip, deflate(, zstd)?$"),
                "connection": "keep-alive",
                "user-agent": "testclient",
                "3": "4",
                "cookie": IsStr(min_length=3),
                "content-length": "9",
                "content-type": "application/json",
                "x-api-version": "2000-01-01",
            },
            "cookies": {"5": "6", "cookie_key": "cookie_val"},
            "query_params": {},
            "body_key": "body_val",
        }
        assert dict(resp.headers) == {
            "content-length": str(len(resp.content)),
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
        _get_endpoint,
    ):
        @convert_response_to_previous_version_for(AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.status_code = 300
            response.headers["header_key"] = "header-val"
            response.set_cookie("cookie_key", "cookie_val", max_age=83)

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients["2000-01-01"].get(test_path)
        assert dict(resp.headers) == {
            "content-length": str(len(resp.content)),
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
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=AnyResponseSchema)
        async def post_endpoint(request: Request):
            return JSONResponse({"hewwo": "darkness"}, status_code=203, headers={"header-key": "header-val"})

        @convert_response_to_previous_version_for(AnyResponseSchema)
        def migrator(response: ResponseInfo):
            assert response.status_code == 203
            assert response.headers["header-key"] == "header-val"
            response.body |= {"migration": "body"}
            response.status_code = 201
            response.headers["header-key"] = "header-val2"

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients["2000-01-01"].post(test_path, json={})
        assert resp.json() == {"hewwo": "darkness", "migration": "body"}
        assert dict(resp.headers) == (
            {
                "header-key": "header-val2",
                "content-length": "39",
                "content-type": "application/json",
                "x-api-version": "2000-01-01",
            }
        )
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients["2001-01-01"].post(test_path, json={})
        assert resp.json() == {"hewwo": "darkness"}
        assert dict(resp.headers) == (
            {
                "header-key": "header-val",
                "content-length": "20",
                "content-type": "application/json",
                "x-api-version": "2001-01-01",
            }
        )
        assert resp.status_code == 203

    def test__fastapi_response_migration__response_only_has_status_code_and_there_is_a_migration(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=AnyResponseSchema)
        async def post_endpoint(request: Request):
            return Response(status_code=200)

        @convert_response_to_previous_version_for(AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.status_code = 201

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients["2000-01-01"].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2000-01-01",
            }
        )
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients["2001-01-01"].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2001-01-01",
            }
        )
        assert resp.status_code == 200

    def test__fastapi_response_migration__streaming_response_and_there_is_a_migration(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=AnyResponseSchema)
        async def post_endpoint(request: Request):
            return StreamingResponse(StringIO("streaming response"), status_code=200)

        @convert_response_to_previous_version_for(AnyResponseSchema)
        def migrator(response: ResponseInfo):
            response.status_code = 201

        clients = create_versioned_clients(version_change(migrator=migrator))
        resp = clients["2000-01-01"].post(test_path, json={})
        assert resp.content == b"streaming response"
        assert dict(resp.headers) == {"x-api-version": "2000-01-01"}
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients["2001-01-01"].post(test_path, json={})
        assert resp.content == b"streaming response"
        assert dict(resp.headers) == {"x-api-version": "2001-01-01"}
        assert resp.status_code == 200

    def test__fastapi_response_migration__response_only_has_status_code_and_there_is_no_migration(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=AnyResponseSchema)
        async def post_endpoint(request: Request):
            return Response(status_code=200)

        clients = create_versioned_clients(version_change())
        resp = clients["2000-01-01"].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2000-01-01",
            }
        )
        assert resp.status_code == 200
        assert dict(resp.cookies) == {}

        resp = clients["2001-01-01"].post(test_path, json={})
        assert resp.content == b""
        assert dict(resp.headers) == (
            {
                "content-length": "0",
                "x-api-version": "2001-01-01",
            }
        )
        assert resp.status_code == 200

    def test__fastapi_response_migration__with_custom_response(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path)
        async def post_endpoint(request: Request):
            return Response(status_code=200, content="Hello, world")

        @convert_response_to_previous_version_for(test_path, ["POST"])
        def converter(response: ResponseInfo):
            assert response.body == "Hello, world"

        client_2000, client_2001 = create_versioned_clients(version_change(converter=converter)).values()
        resp = client_2000.post(test_path, json={})
        assert resp.content == b"Hello, world"
        assert resp.status_code == 200

        resp = client_2001.post(test_path, json={})
        assert resp.content == b"Hello, world"
        assert resp.status_code == 200

    def test__response_migration__when_response_model_is_unhashable__should_migrate_by_path(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        # Dict metadata inside Annotated makes the whole annotation unhashable
        @router.get(test_path)
        async def route() -> list[Annotated[int, {"description": "item"}]]:
            return [1, 2]

        @convert_response_to_previous_version_for(test_path, ["GET"])
        def migrator(response: ResponseInfo):
            response.body.append(3)

        clients = create_versioned_clients(version_change(migrator=migrator))
        assert clients["2000-01-01"].get(test_path).json() == [1, 2, 3]
        assert clients["2001-01-01"].get(test_path).json() == [1, 2]

    @pytest.mark.parametrize(
        ("response_model", "head_body", "migrated_body"),
        [
            pytest.param(int, 2, 1, id="int"),
            pytest.param(dict[str, int], {"a": 2}, {"a": 1}, id="dict"),
            pytest.param(TypedDictResponse, {"foo": 2}, {"foo": 1}, id="TypedDict"),
            pytest.param(list[SchemaWithInternalRepresentation], [{"foo": 2}], [{"foo": 1}], id="list[Model]"),
            pytest.param(Optional[SchemaWithInternalRepresentation], {"foo": 2}, {"foo": 1}, id="Optional[Model]"),
            pytest.param(
                Annotated[SchemaWithInternalRepresentation, "metadata"], {"foo": 2}, {"foo": 1}, id="Annotated[Model]"
            ),
        ],
    )
    def test__response_migration__by_non_model_hashable_schema__should_migrate(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
        response_model: Any,
        head_body: Any,
        migrated_body: Any,
    ):
        @router.get(test_path, response_model=response_model)
        async def route():
            return head_body

        @convert_response_to_previous_version_for(response_model, check_usage=False)
        def migrator(response: ResponseInfo):
            response.body = migrated_body

        clients = create_versioned_clients(version_change(migrator=migrator))
        assert clients["2000-01-01"].get(test_path).json() == migrated_body
        assert clients["2001-01-01"].get(test_path).json() == head_body


class TestHowAndWhenMigrationsApply:
    def test__migrate_request_and_response__with_no_migrations__should_not_raise_error(
        self,
        test_path: Literal["/test"],
        create_versioned_clients: CreateVersionedClients,
        _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    ):
        clients = create_versioned_clients()
        assert clients["2000-01-01"].post(test_path, json={"A": "B"}).json() == {
            "body": {"A": "B"},
            "headers": IsPartialDict(),
            "cookies": {},
            "query_params": {},
        }

    def test__migrate_request__with_no_migrations__request_schema_should_be_from_latest(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=AnyResponseSchema)
        async def endpoint(foo: AnyRequestSchema):
            assert isinstance(foo, AnyRequestSchema), f"Request schema is from: {foo.__class__.__module__}"
            return {}

        clients = create_versioned_clients(version_change(), version_change())
        resp_2000 = clients["2000-01-01"].post(test_path, json={})
        assert resp_2000.status_code, resp_2000.json()

        resp_2001 = clients["2001-01-01"].post(test_path, json={})
        assert resp_2001.status_code, resp_2001.json()

        resp_2002 = clients["2002-01-01"].post(test_path, json={})
        assert resp_2002.status_code, resp_2002.json()

    def test__migrate_one_version_down__migrations_are_applied_to_2000_version_but_not_to_2000(
        self,
        version_change_1: type[VersionChange],
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1)
        assert clients["2000-01-01"].post(test_path, json=[]).json()["body"] == [
            "request change 1",
            "response change 1",
        ]
        assert clients["2001-01-01"].post(test_path, json=[]).json()["body"] == []

    def test__migrate_two_versions_down__2002_applies_to_2001_and_2000_while_2001_only_applies_to_2000(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        assert clients["2000-01-01"].post(test_path, json=[]).json()["body"] == [
            "request change 1",
            "request change 2",
            "response change 2",
            "response change 1",
        ]
        assert clients["2001-01-01"].post(test_path, json=[]).json()["body"] == [
            "request change 2",
            "response change 2",
        ]
        assert clients["2002-01-01"].post(test_path, json=[]).json()["body"] == []

    def test__try_migrating_when_version_is_none__no_migrations_get_applied(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        api_version_var: ContextVar[Union[str, None]],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        app = clients["2000-01-01"].app
        none_client = client(
            APIRouter(routes=app.router.versioned_routers["2000-01-01"].routes),
            api_version=None,
            api_version_var=api_version_var,
        )
        # The version below is not actually used anywhere, but it's required so we pass a dummy one
        assert (
            none_client.post(
                test_path,
                json=[],
                headers={app.router.api_version_parameter_name: "2000-11-11"},
            ).json()["body"]
            == []
        )

    # TODO: An error is a better behavior here
    def test__try_migrating_to_version_below_earliest__should_apply_all_migrations(
        self,
        create_versioned_clients: CreateVersionedClients,
        version_change_1: type[VersionChange],
        version_change_2: type[VersionChange],
        test_path: str,
        api_version_var: ContextVar[Union[str, None]],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        app = clients["2000-01-01"].app
        api_version_var.set("1998-02-10")

        assert clients["2000-01-01"].post(
            test_path,
            json=[],
            headers={app.router.api_version_parameter_name: "2000-01-01"},
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
        api_version_var: ContextVar[Union[str, None]],
        _post_endpoint,
    ):
        clients = create_versioned_clients(version_change_1, version_change_2)
        app = clients["2000-01-01"].app
        assert (
            clients["2000-01-01"]
            .post(test_path, json=[], headers={app.router.api_version_parameter_name: "2050-01-01"})
            .json()["body"]
            == []
        )

    def test__cookies_can_be_deleted_during_migrations(
        self,
        create_versioned_clients: CreateVersionedClients,
        test_path: Literal["/test"],
        router: VersionedAPIRouter,
    ):
        @router.post(test_path, response_model=AnyResponseSchema)
        async def endpoint(response: Response):
            response.set_cookie("cookie_key", "cookie_val")
            return 83

        @convert_response_to_previous_version_for(AnyResponseSchema)
        def migration(response: ResponseInfo):
            response.delete_cookie("cookie_key")

        clients = create_versioned_clients(version_change(migration=migration))
        resp_2000 = clients["2000-01-01"].post(test_path, json={})
        resp_2001 = clients["2001-01-01"].post(test_path, json={})

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
            # https://github.com/python/cpython/pull/112714
            **({"partitioned": ""} if sys.version_info >= (3, 14) else {}),
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
        TypeError,
        match=re.escape("If path was provided as a first argument, methods must be provided as a second argument"),
    ):
        convert_request_to_next_version_for("/test")  # ty: ignore[invalid-argument-type]


def test__schema_migration_syntax__with_methods_after_a_schema__should_raise_error():
    with pytest.raises(
        TypeError,
        match=re.escape("If schema was provided as a first argument, all other arguments must also be schemas"),
    ):
        convert_request_to_next_version_for(AnyRequestSchema, ["POST"])  # ty: ignore[no-matching-overload]


def test__schema_migration_syntax__with_additional_schemas_after_methods__should_raise_error():
    with pytest.raises(
        TypeError,
        match=re.escape("If path was provided as a first argument, then additional schemas cannot be added"),
    ):
        convert_request_to_next_version_for("/v1/test", ["POST"], AnyRequestSchema)  # ty: ignore[invalid-argument-type]


def test__uploadfile_can_work(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, response_model=AnyResponseSchema)
    async def endpoint(file: UploadFile = File(...)):
        # PydanticV2 can no longer serialize files directly like it could in v1
        file_dict = {k: v for k, v in file.__dict__.items() if not k.startswith("_") and k != "file"}
        file_dict["headers"] = dict(file_dict["headers"])
        return file_dict

    clients = create_versioned_clients(version_change())
    resp_2000 = clients["2000-01-01"].post(test_path, files={"file": b"Hewwo"})
    resp_2001 = clients["2001-01-01"].post(test_path, files={"file": b"Hewwo"})

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


def test__form_migration__can_modify_string_form_field(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(name: str = Form(...), age: int = Form(...)):
        return {"name": name, "age": age}

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        request.form[:] = [(k, "migrated_name") if k == "name" else (k, v) for k, v in request.form]

    clients = create_versioned_clients(version_change(migrator=migrator))
    resp_2000 = clients["2000-01-01"].post(test_path, data={"name": "original_name", "age": "25"})
    resp_2001 = clients["2001-01-01"].post(test_path, data={"name": "original_name", "age": "25"})

    assert resp_2000.json() == {"name": "migrated_name", "age": 25}
    assert resp_2001.json() == {"name": "original_name", "age": 25}


def test__form_migration__form_is_none_for_non_form_requests(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, response_model=AnyResponseSchema)
    async def endpoint(payload: AnyRequestSchema):
        return payload

    captured = []

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        captured.append(request.form)

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].post(test_path, json={"foo": "bar"})

    assert captured == [None]


def test__form_migration__can_add_new_form_field(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(name: str = Form(...), extra: str = Form("default")):
        return {"name": name, "extra": extra}

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        request.form.append(("extra", "added_by_migration"))

    clients = create_versioned_clients(version_change(migrator=migrator))
    resp_2000 = clients["2000-01-01"].post(test_path, data={"name": "my_name"})
    resp_2001 = clients["2001-01-01"].post(test_path, data={"name": "my_name"})

    assert resp_2000.json() == {"name": "my_name", "extra": "added_by_migration"}
    assert resp_2001.json() == {"name": "my_name", "extra": "default"}


def test__form_migration__multi_value_fields_are_preserved(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(tags: list[str] = Form(...)):  # pragma: no cover
        return {"tags": tags}

    clients = create_versioned_clients(version_change())
    resp_2000 = clients["2000-01-01"].post(
        test_path,
        content=b"tags=python&tags=web",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    resp_2001 = clients["2001-01-01"].post(
        test_path,
        content=b"tags=python&tags=web",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert resp_2000.json() == {"tags": ["python", "web"]}
    assert resp_2001.json() == {"tags": ["python", "web"]}


def test__request_body_parsing__empty_body_with_optional_params(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(name: Optional[str] = Body(default=None), age: Optional[int] = Body(default=None)):
        return {"name": name, "age": age}

    clients = create_versioned_clients(version_change())
    resp = clients["2000-01-01"].post(
        test_path,
        content=b"",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"name": None, "age": None}


async def _nullable_body_with_default(payload: Optional[dict[str, int]] = Body({"default": 1})):
    return {"payload": payload}


async def _required_nullable_body(payload: Optional[dict[str, int]] = Body()):
    return {"payload": payload}


async def _non_nullable_body_with_default(payload: dict[str, int] = Body({"default": 1})):
    return {"payload": payload}


async def _non_nullable_body_with_none_default(payload: dict[str, int] = Body(None)):
    return {"payload": payload}


async def _optional_embedded_body(payload: Optional[dict[str, int]] = Body(None, embed=True)):
    return {"payload": payload}


async def _optional_embedded_bodies(name: Optional[str] = Body(None), age: Optional[int] = Body(None)):
    return {"name": name, "age": age}


async def _optional_file(payload: Optional[bytes] = File(None)):
    return {"payload": payload if payload is None else payload.decode()}


def _empty_string_as_none(value: Any) -> Any:
    return None if value == "" else value


async def _validated_body_with_default(
    payload: Annotated[Optional[dict[str, int]], BeforeValidator(_empty_string_as_none), Body()] = {"default": 1},  # noqa: B006
):
    return {"payload": payload}


async def _validated_required_body(
    payload: Annotated[Optional[dict[str, int]], BeforeValidator(_empty_string_as_none), Body()],
):
    return {"payload": payload}


def _raw_null_as_none(value: Any) -> Any:  # pragma: no cover # FastAPI >= 0.151
    return None if value == b"null" else value


async def _raw_validated_body_with_default(  # pragma: no cover # FastAPI >= 0.151
    payload: Annotated[Optional[dict[str, int]], BeforeValidator(_raw_null_as_none), Body()] = {"default": 1},  # noqa: B006
):
    return {"payload": payload}


async def _raw_validated_required_body(  # pragma: no cover # FastAPI >= 0.151
    payload: Annotated[Optional[dict[str, int]], BeforeValidator(_raw_null_as_none), Body()],
):
    return {"payload": payload}


async def _read_json_body(request: Request) -> None:
    if await request.body():
        await request.json()


@pytest.mark.parametrize(
    "endpoint",
    [
        _nullable_body_with_default,
        _required_nullable_body,
        _non_nullable_body_with_default,
        _non_nullable_body_with_none_default,
        _optional_embedded_body,
        _optional_embedded_bodies,
        _optional_file,
        _validated_body_with_default,
        _validated_required_body,
    ],
)
@pytest.mark.parametrize("content", [b"", b"null", b'{"a": 1}'])
@pytest.mark.parametrize("headers", [{"content-type": "application/json"}, {}])
# FastAPI only parses bodies without a content type as JSON with `strict_content_type=False`
@pytest.mark.parametrize("strict_content_type", [True, False])
# Starlette caches the JSON that a dependency parses, which must not change how Cadwyn sees the body
@pytest.mark.parametrize("dependencies", [[], [Depends(_read_json_body)]])
def test__request_body_parsing__omitted_null_and_present_bodies__should_be_handled_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    content: bytes,
    headers: dict[str, str],
    strict_content_type: bool,
    dependencies: list[Any],
):
    # FastAPI 0.151.0 and newer validate a JSON null body as None and apply defaults only to omitted bodies
    router = VersionedAPIRouter(strict_content_type=strict_content_type)
    router.post(test_path, dependencies=dependencies)(endpoint)
    fastapi_app = fastapi.FastAPI(strict_content_type=strict_content_type)
    fastapi_app.post(test_path, dependencies=dependencies)(endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    expected = TestClient(fastapi_app).post(test_path, content=content, headers=headers)
    for version_client in clients.values():
        response = version_client.post(test_path, content=content, headers=headers)
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.151
    not FASTAPI_DISTINGUISHES_NULL_BODIES, reason="Before FastAPI 0.151.0, a body that is None is an omitted one"
)
@pytest.mark.parametrize(
    ("endpoint", "content", "headers"),
    [
        (_validated_body_with_default, b'""', {"content-type": "application/json"}),
        (_validated_required_body, b'""', {"content-type": "application/json"}),
        (_raw_validated_body_with_default, b"null", {"content-type": "text/plain"}),
        (_raw_validated_required_body, b"null", {"content-type": "text/plain"}),
    ],
)
def test__request_body_parsing__body_that_validators_turn_into_none__should_be_present_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    content: bytes,
    headers: dict[str, str],
):
    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    expected = TestClient(fastapi_app).post(test_path, content=content, headers=headers)
    assert (expected.status_code, expected.json()) == (200, {"payload": None})
    for version_client in clients.values():
        response = version_client.post(test_path, content=content, headers=headers)
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())


def test__request_body_parsing__non_application_content_type(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(name: Optional[str] = Body(default=None), age: Optional[int] = Body(default=None)):
        return {"name": name, "age": age}

    clients = create_versioned_clients(version_change())
    resp = clients["2000-01-01"].post(
        test_path,
        content=b'{"name": "test", "age": 25}',
        headers={"content-type": "text/plain"},
    )

    assert resp.status_code in [200, 422]


def test__request_body_parsing__application_xml_body(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(name: Optional[str] = Body(default=None), age: Optional[int] = Body(default=None)):
        return {"name": name, "age": age}

    clients = create_versioned_clients(version_change())
    resp = clients["2000-01-01"].post(
        test_path,
        content=b"<root/>",
        headers={"content-type": "application/xml"},
    )

    assert resp.status_code in [200, 422]


def test__request_body_parsing__non_json_application_body_is_preserved(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: Optional[bytes] = Body(default=None), was_migrated: bool = Body(default=False)):
        return {"body": list(body or b""), "was_migrated": was_migrated}

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrate_raw_body(request: RequestInfo):
        request.body = {"body": request.body, "was_migrated": True}

    clients = create_versioned_clients(version_change(migrate_raw_body=migrate_raw_body))
    body = b"\x00cadwyn\xff"
    resp = clients["2000-01-01"].post(
        test_path,
        content=body,
        headers={"content-type": "application/octet-stream"},
    )

    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"body": list(body), "was_migrated": True}


def test__request_body_parsing__application_vendor_json_is_parsed(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(answer: int = Body(), version: int = Body()):
        return {"answer": answer, "version": version}

    clients = create_versioned_clients(version_change())
    resp = clients["2000-01-01"].post(
        test_path,
        content=b'{"answer": 42, "version": 7}',
        headers={"content-type": "application/vnd.cadwyn+json"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"answer": 42, "version": 7}


def test__request_body_parsing__malformed_json_returns_422(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(name: Optional[str] = Body(default=None), age: Optional[int] = Body(default=None)):
        return {"name": name, "age": age}  # pragma: no cover

    clients = create_versioned_clients(version_change())
    assert clients["2000-01-01"].post(test_path, json={"name": "Bob", "age": 42}).json() == {
        "name": "Bob",
        "age": 42,
    }

    resp = clients["2000-01-01"].post(
        test_path,
        content=b"not valid json",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "json_invalid"


def _model_with_value_field(
    field: Callable[[], Any],
    *,
    annotated: bool,
    config: Optional[ConfigDict] = None,
) -> type[BaseModel]:
    if annotated:

        class AnnotatedModel(BaseModel):
            model_config = config or ConfigDict()
            value: Annotated[int, field()]

        return AnnotatedModel

    class DefaultModel(BaseModel):
        model_config = config or ConfigDict()
        value: int = field()

    return DefaultModel


# (field, config, validation payload, key of `model_dump(by_alias=True)`)
_FIELDS_WITH_ANY_KIND_OF_ALIASES = [
    pytest.param(lambda: Field(alias="x"), None, {"x": 1}, "x", id="alias"),
    pytest.param(lambda: Field(alias="x", validation_alias="v"), None, {"v": 1}, "x", id="alias+validation_alias"),
    pytest.param(
        lambda: Field(validation_alias="v", serialization_alias="s"),
        None,
        {"v": 1},
        "s",
        id="validation_alias+serialization_alias",
    ),
    pytest.param(lambda: Field(validation_alias="v"), None, {"v": 1}, "value", id="validation_alias"),
    pytest.param(lambda: Field(serialization_alias="s"), None, {"value": 1}, "s", id="serialization_alias"),
    pytest.param(
        lambda: Field(alias="x", serialization_alias="s"), None, {"x": 1}, "s", id="alias+serialization_alias"
    ),
    pytest.param(lambda: Field(validation_alias=AliasChoices("v", "w")), None, {"w": 1}, "value", id="alias_choices"),
    pytest.param(
        lambda: Field(validation_alias=AliasChoices(AliasPath("p", 0), "w")),
        None,
        {"p": [1]},
        "value",
        id="alias_choices_with_path_and_key",
    ),
    pytest.param(
        lambda: Field(alias="x", validation_alias=AliasChoices("v", "x")),
        None,
        {"v": 1},
        "x",
        id="alias_choices_with_serialization_alias",
    ),
    pytest.param(
        Field,
        ConfigDict(alias_generator=str.upper),
        {"VALUE": 1},
        "VALUE",
        id="alias_generator",
    ),
    pytest.param(
        Field,
        ConfigDict(alias_generator=AliasGenerator(validation_alias=str.upper, serialization_alias=str.title)),
        {"VALUE": 1},
        "Value",
        id="alias_generator_with_different_aliases",
    ),
    pytest.param(
        lambda: Field(validation_alias="v"),
        ConfigDict(populate_by_name=True),
        {"v": 1},
        "value",
        id="populate_by_name+validation_alias",
    ),
    pytest.param(
        lambda: Field(alias="x", validation_alias="v"),
        ConfigDict(populate_by_name=True),
        {"value": 1},
        "x",
        id="populate_by_name+alias+validation_alias",
    ),
    pytest.param(
        lambda: Field(alias="x", validation_alias="v"),
        ConfigDict(validate_by_name=True),
        {"v": 1},
        "x",
        id="validate_by_name+alias+validation_alias",
    ),
    pytest.param(
        lambda: Field(alias="x"),
        ConfigDict(validate_by_name=True, validate_by_alias=False),
        {"value": 1},
        "x",
        id="validate_by_name_only+alias",
    ),
]


@pytest.mark.parametrize("annotated", [False, True], ids=["field_default", "annotated"])
@pytest.mark.parametrize(("field", "config", "payload", "dumped_key"), _FIELDS_WITH_ANY_KIND_OF_ALIASES)
def test__request_body_migration__with_any_kind_of_field_aliases__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotated: bool,
    field: Callable[[], Any],
    config: Optional[ConfigDict],
    payload: dict[str, Any],
    dumped_key: str,
):
    model = _model_with_value_field(field, annotated=annotated, config=config)

    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return body.value

    bodies_seen_by_migration = []

    @convert_request_to_next_version_for(model)
    def migrator(request: RequestInfo):
        bodies_seen_by_migration.append(request.body)

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json=payload).json() == 1
    assert clients["2001-01-01"].post(test_path, json=payload).json() == 1
    # Migrations still see the keys of `model_dump(by_alias=True)`
    assert bodies_seen_by_migration == [{dumped_key: 1}]


class VariablePostBody(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="forbid")

    value: Optional[str] = Field(serialization_alias="val")
    description: Optional[str] = Field(default=None)


def test__request_body_migration__with_required_field_with_serialization_alias_and_forbidden_extra_keys__should_work(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.put(test_path, status_code=201)
    async def endpoint(body: VariablePostBody):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    # The head surely rejects the dumped body as it is, so we restore it
    for version_client in clients.values():
        assert version_client.put(test_path, json={"value": "new_value"}).json() == {
            "value": "new_value",
            "description": None,
        }


class InnerSchemaWithValidationAlias(BaseModel):
    value: int = Field(alias="x", validation_alias="v")


class OtherInnerSchema(BaseModel):
    other_value: int = Field(alias="o")


@pytest.mark.parametrize(
    ("annotation", "payload", "expected"),
    [
        pytest.param(InnerSchemaWithValidationAlias, {"v": 1}, {"value": 1}, id="model"),
        pytest.param(Optional[InnerSchemaWithValidationAlias], {"v": 1}, {"value": 1}, id="optional_model"),
        pytest.param(Optional[InnerSchemaWithValidationAlias], None, None, id="optional_model_with_none"),
        pytest.param(
            list[InnerSchemaWithValidationAlias], [{"v": 1}, {"v": 2}], [{"value": 1}, {"value": 2}], id="list"
        ),
        pytest.param(
            list[Annotated[InnerSchemaWithValidationAlias, Field(description="Inner")]],
            [{"v": 1}],
            [{"value": 1}],
            id="list_of_annotated",
        ),
        pytest.param(
            dict[str, InnerSchemaWithValidationAlias],
            {"a": {"v": 1}, "b": {"v": 2}},
            {"a": {"value": 1}, "b": {"value": 2}},
            id="dict",
        ),
        pytest.param(Optional[dict[str, InnerSchemaWithValidationAlias]], None, None, id="optional_dict_with_none"),
        pytest.param(Optional[list[InnerSchemaWithValidationAlias]], [{"v": 1}], [{"value": 1}], id="optional_list"),
        pytest.param(Optional[list[InnerSchemaWithValidationAlias]], None, None, id="optional_list_with_none"),
        pytest.param(list[list[InnerSchemaWithValidationAlias]], [[{"v": 1}], []], [[{"value": 1}], []], id="lists"),
        pytest.param(
            list[Annotated[InnerSchemaWithValidationAlias, {"unhashable": "metadata"}]],
            [{"v": 1}],
            [{"value": 1}],
            id="list_of_unhashable_annotated",
        ),
        pytest.param(typing.List, [{"v": 1}], [{"v": 1}], id="bare_list"),  # noqa: UP006
        pytest.param(typing.Dict, {"a": {"v": 1}}, {"a": {"v": 1}}, id="bare_dict"),  # noqa: UP006
        pytest.param(
            Union[OtherInnerSchema, InnerSchemaWithValidationAlias],
            {"o": 1},
            {"other_value": 1},
            id="union_of_models",
        ),
    ],
)
def test__request_body_migration__with_validation_aliases_in_nested_models__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
    payload: Any,
    expected: Any,
):
    class OuterSchema(BaseModel):
        inner: annotation = Field(alias="i", validation_alias="j")
        unset_inner: Optional[InnerSchemaWithValidationAlias] = None

    @router.post(test_path)
    async def endpoint(body: OuterSchema):
        return body.model_dump()

    @convert_request_to_next_version_for(OuterSchema)
    def migrator(request: RequestInfo):
        request.body["i"] = request.body["i"]

    clients = create_versioned_clients(version_change(migrator=migrator))

    for version_client in clients.values():
        assert version_client.post(test_path, json={"j": payload}).json() == {"inner": expected, "unset_inner": None}


class RootSchemaWithValidationAliases(RootModel[list[InnerSchemaWithValidationAlias]]):
    pass


@pytest.mark.parametrize(
    "annotation",
    [Optional[InnerSchemaWithValidationAlias], Annotated[InnerSchemaWithValidationAlias, Body()]],
)
def test__request_body_migration__with_validation_aliases_in_top_level_body__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
):
    @router.post(test_path)
    async def endpoint(body: annotation = None):
        return body.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"v": 1}).json() == 1


def test__request_body_migration__with_validation_aliases_in_root_model__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: RootSchemaWithValidationAliases):
        return [inner.value for inner in body.root]

    @convert_request_to_next_version_for(RootSchemaWithValidationAliases)
    def migrator(request: RequestInfo):
        request.body.append({"x": 3})

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json=[{"v": 1}, {"v": 2}]).json() == [1, 2, 3]
    assert clients["2001-01-01"].post(test_path, json=[{"v": 1}, {"v": 2}]).json() == [1, 2]


def test__request_body_migration__with_validation_aliases_in_form_model__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: Annotated[InnerSchemaWithValidationAlias, Form()]):
        return body.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, data={"v": "1"}).json() == 1


def test__request_body_migration__with_optional_form_model__head_should_get_the_value_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None):
        return None if body is None else body.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, data={"v": "1"}).json() == (
            1 if FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS else None
        )


@pytest.mark.parametrize(
    "alias_choices",
    [AliasChoices("p", "q"), AliasChoices("p", AliasPath("q", "r"))],
    ids=["names", "names_and_paths"],
)
def test__request_body_migration__with_alias_choices_of_form_model_param__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    alias_choices: AliasChoices,
):
    @router.post(test_path)
    async def endpoint(body: Annotated[InnerSchemaWithValidationAlias, Form(validation_alias=alias_choices)]):
        return body.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, data={"v": "1"}).json() == 1


async def _get_value_from_form_under_other_alias(
    body: Annotated[InnerSchemaWithValidationAlias, Form(alias="other")],
) -> int:
    return body.value


def test__request_body_migration__with_form_model_shared_by_dependency_under_other_alias__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(
        body: Annotated[InnerSchemaWithValidationAlias, Form()],
        from_dependency: int = Depends(_get_value_from_form_under_other_alias),
    ):
        return [body.value, from_dependency]

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, data={"v": "1"}).json() == [1, 1]


async def _get_value() -> int:
    raise NotImplementedError


def test__request_body_migration__with_form_model_shared_by_dependency_override__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(
        body: Annotated[InnerSchemaWithValidationAlias, Form()], from_dependency: int = Depends(_get_value)
    ):
        return [body.value, from_dependency]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_value] = _get_value_from_form_under_other_alias

    for version_client in clients.values():
        assert version_client.post(test_path, data={"v": "1"}).json() == [1, 1]


async def _get_value_from_form_field(value: int = Form(alias="v")) -> int:
    return value  # pragma: no cover # FastAPI >= 0.158


def test__request_body_migration__with_form_model_and_form_field_of_dependency_override__head_should_get_both(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(
        body: Annotated[InnerSchemaWithValidationAlias, Form()], from_dependency: int = Depends(_get_value)
    ):
        return [body.value, from_dependency]  # pragma: no cover # FastAPI >= 0.158

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_value] = _get_value_from_form_field

    for version_client in clients.values():
        response = version_client.post(test_path, data={"v": "1"})
        assert response.json() == (
            [1, 1]
            if FASTAPI_VALIDATES_FORM_FIELDS_OF_OVERRIDES
            else {
                "detail": [
                    {
                        "type": "int_type",
                        "loc": ["body"],
                        "msg": "Input should be a valid integer",
                        "input": {"v": "1"},
                    }
                ]
            }
        )


class SchemaWithUpload(BaseModel):
    upload: UploadFile


async def _get_body_with_upload(upload: UploadFile = File()) -> SchemaWithUpload:
    return SchemaWithUpload(upload=upload)


def test__request_body_migration__with_upload_and_dependency_named_like_the_body__head_should_get_the_upload(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: UploadFile = File(), body: SchemaWithUpload = Depends(_get_body_with_upload)):
        return (await upload.read()).decode()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": b"first"}).json() == "first"


@pytest.mark.parametrize("annotated", [False, True], ids=["field_default", "annotated"])
@pytest.mark.parametrize(
    ("field", "config", "payload", "dumped_key"),
    [
        *_FIELDS_WITH_ANY_KIND_OF_ALIASES,
        pytest.param(
            lambda: Field(alias="x", validation_alias="v"),
            ConfigDict(serialize_by_alias=True),
            {"v": 1},
            "x",
            id="serialize_by_alias+alias+validation_alias",
        ),
    ],
)
def test__response_migration__with_any_kind_of_field_aliases__client_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotated: bool,
    field: Callable[[], Any],
    config: Optional[ConfigDict],
    payload: dict[str, Any],
    dumped_key: str,
):
    model = _model_with_value_field(field, annotated=annotated, config=config)

    @router.get(test_path, response_model=model)
    async def endpoint():
        return model.model_validate(payload)

    bodies_seen_by_migration = []

    @convert_response_to_previous_version_for(model)
    def migrator(response: ResponseInfo):
        bodies_seen_by_migration.append(response.body)

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].get(test_path).json() == {dumped_key: 1}
    assert clients["2001-01-01"].get(test_path).json() == {dumped_key: 1}
    # Migrations still see the keys of `model_dump(by_alias=True)`
    assert bodies_seen_by_migration == [{dumped_key: 1}]


def test__response_migration__with_validation_alias_and_response_model_by_alias_false__client_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=InnerSchemaWithValidationAlias, response_model_by_alias=False)
    async def endpoint():
        return InnerSchemaWithValidationAlias.model_validate({"v": 1})

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.get(test_path).json() == {"value": 1}


class OuterSchemaWithValidationAliases(BaseModel):
    inner: InnerSchemaWithValidationAlias = Field(alias="i", validation_alias="j")
    items: list[InnerSchemaWithValidationAlias] = []
    mapping: dict[str, InnerSchemaWithValidationAlias] = {}
    optional: Optional[InnerSchemaWithValidationAlias] = None


class SubclassOfInnerSchemaWithValidationAlias(InnerSchemaWithValidationAlias):
    other: int = 2

    @computed_field
    @property
    def doubled(self) -> int:
        return self.value * 2


class SchemaWithFieldValidatorAndValidationAlias(BaseModel):
    value: int = Field(alias="x", validation_alias="v")

    @field_validator("value")
    @classmethod
    def check(cls, value: int) -> int:
        return value


class SubclassOfSchemaWithFieldValidatorAndValidationAlias(SchemaWithFieldValidatorAndValidationAlias):
    other: int = 2


def _inner(value: int) -> InnerSchemaWithValidationAlias:
    return InnerSchemaWithValidationAlias.model_validate({"v": value})


@pytest.mark.parametrize(
    ("response_model", "get_response", "expected"),
    [
        pytest.param(InnerSchemaWithValidationAlias, lambda: _inner(1), {"x": 1}, id="model"),
        pytest.param(
            InnerSchemaWithValidationAlias,
            lambda: SubclassOfInnerSchemaWithValidationAlias.model_validate({"v": 1}),
            {"x": 1},
            id="subclass",
        ),
        pytest.param(
            SchemaWithFieldValidatorAndValidationAlias,
            lambda: SubclassOfSchemaWithFieldValidatorAndValidationAlias.model_validate({"v": 1}),
            {"x": 1},
            id="subclass_of_model_with_field_validator",
        ),
        pytest.param(Optional[InnerSchemaWithValidationAlias], lambda: _inner(1), {"x": 1}, id="optional_model"),
        pytest.param(Optional[InnerSchemaWithValidationAlias], lambda: None, None, id="optional_model_with_none"),
        pytest.param(
            list[InnerSchemaWithValidationAlias], lambda: [_inner(1), _inner(2)], [{"x": 1}, {"x": 2}], id="list"
        ),
        pytest.param(dict[str, InnerSchemaWithValidationAlias], lambda: {"a": _inner(1)}, {"a": {"x": 1}}, id="dict"),
        pytest.param(
            list[list[InnerSchemaWithValidationAlias]],
            lambda: [[_inner(1)], []],
            [[{"x": 1}], []],
            id="lists",
        ),
        pytest.param(Optional[list[InnerSchemaWithValidationAlias]], lambda: None, None, id="optional_list_with_none"),
        pytest.param(
            list[Optional[InnerSchemaWithValidationAlias]],
            lambda: [_inner(1), None],
            [{"x": 1}, None],
            id="list_of_optional_models",
        ),
        pytest.param(
            dict[str, Optional[list[InnerSchemaWithValidationAlias]]],
            lambda: {"a": [_inner(1)], "b": None},
            {"a": [{"x": 1}], "b": None},
            id="dict_of_optional_lists",
        ),
        pytest.param(
            RootSchemaWithValidationAliases,
            lambda: RootSchemaWithValidationAliases([_inner(1)]),
            [{"x": 1}],
            id="root_model",
        ),
        pytest.param(RootSchemaWithValidationAliases, lambda: [_inner(1)], [{"x": 1}], id="list_as_root_model"),
        pytest.param(
            OuterSchemaWithValidationAliases,
            lambda: OuterSchemaWithValidationAliases.model_validate(
                {"j": {"v": 1}, "items": [{"v": 2}], "mapping": {"a": {"v": 3}}, "optional": {"v": 4}}
            ),
            {"i": {"x": 1}, "items": [{"x": 2}], "mapping": {"a": {"x": 3}}, "optional": {"x": 4}},
            id="nested_models",
        ),
    ],
)
def test__response_migration__with_validation_aliases_in_nested_models__client_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    response_model: Any,
    get_response: Callable[[], Any],
    expected: Any,
):
    @router.get(test_path, response_model=response_model)
    async def endpoint():
        return get_response()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.get(test_path).json() == expected


class SchemaWithDefaultAndValidationAlias(BaseModel):
    value: int = Field(default=0, alias="x", validation_alias="v")


class SchemaWithTheDumpedKeyOfAnotherSchema(BaseModel):
    x: str = "not an int"


class SchemaWithRequiredValidationAlias(BaseModel):
    value: int = Field(alias="x", validation_alias="v")


# The response fails like it did before
FAILS_AS_BEFORE = object()


class PageWithItemsThatAreOmittedOnError(BaseModel):
    items: list[OnErrorOmit[SchemaWithRequiredValidationAlias]] = []
    total: int = 0


class MappingWithItemsThatAreOmittedOnError(BaseModel):
    items: dict[str, OnErrorOmit[SchemaWithRequiredValidationAlias]] = {}


class RootSchemaWithRequiredValidationAlias(RootModel[list[SchemaWithRequiredValidationAlias]]):
    pass


class SubclassOfRootSchemaWithRequiredValidationAlias(RootSchemaWithRequiredValidationAlias):
    pass


class SubclassWithFieldSerializer(SchemaWithRequiredValidationAlias):
    @field_serializer("value")
    def redact(self, value: int) -> str:
        return "redacted"


class SubclassWithModelSerializer(SchemaWithRequiredValidationAlias):
    @model_serializer(mode="wrap")
    def serialize(self, handler: Callable[[Any], Any]) -> Any:
        return {**handler(self), "x": "redacted"}


class SubclassWithModelDumpOverride(SchemaWithRequiredValidationAlias):
    @override
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {**super().model_dump(**kwargs), "x": "redacted"}


class SubclassWithRetypedField(SchemaWithRequiredValidationAlias):
    value: str = Field(default="text", alias="x", validation_alias="v")  # ty: ignore[invalid-attribute-override]


class SubclassWithFieldOnTheDumpedKey(SchemaWithRequiredValidationAlias):
    other: str = Field(default="text", serialization_alias="x")


class SubclassWithComputedFieldOnTheDumpedKey(SchemaWithRequiredValidationAlias):
    @computed_field(alias="x")
    @property
    def other(self) -> str:
        return "text"


class SubclassWithExtraAllow(SchemaWithRequiredValidationAlias):
    model_config = ConfigDict(extra="allow")


def _subclass_instance(cls: type[SchemaWithRequiredValidationAlias]) -> SchemaWithRequiredValidationAlias:
    return cls.model_validate({"v": 1})


@pytest.mark.parametrize(
    ("response_model", "get_response", "expected"),
    [
        # The response passes validation with the default, so we keep it as it is
        pytest.param(
            SchemaWithDefaultAndValidationAlias,
            lambda: SchemaWithDefaultAndValidationAlias.model_validate({"v": 1}),
            {"x": 0},
            id="response_that_passes_validation",
        ),
        # `OnErrorOmit` omits the items that fail validation, so the response passes as it is
        pytest.param(
            list[OnErrorOmit[SchemaWithRequiredValidationAlias]],
            lambda: [SchemaWithRequiredValidationAlias.model_validate({"v": 1})],
            [],
            id="list_of_items_that_are_omitted_on_error",
        ),
        pytest.param(
            PageWithItemsThatAreOmittedOnError,
            lambda: PageWithItemsThatAreOmittedOnError(
                items=[SchemaWithRequiredValidationAlias.model_validate({"v": 1})], total=1
            ),
            {"items": [], "total": 1},
            id="nested_list_of_items_that_are_omitted_on_error",
        ),
        pytest.param(
            MappingWithItemsThatAreOmittedOnError,
            lambda: MappingWithItemsThatAreOmittedOnError(
                items={"a": SchemaWithRequiredValidationAlias.model_validate({"v": 1})}
            ),
            {"items": {}},
            id="nested_dict_of_items_that_are_omitted_on_error",
        ),
        # FastAPI validates the dicts that the endpoint returns as they are
        pytest.param(SchemaWithRequiredValidationAlias, lambda: {"x": 1}, FAILS_AS_BEFORE, id="dict"),
        pytest.param(
            list[SchemaWithRequiredValidationAlias],
            lambda: [SchemaWithRequiredValidationAlias.model_validate({"v": 1}), {"x": 2}],
            FAILS_AS_BEFORE,
            id="list_of_model_and_dict",
        ),
        # The dump of another model can have the keys of the response model with values that it doesn't accept
        pytest.param(
            SchemaWithRequiredValidationAlias,
            lambda: SchemaWithTheDumpedKeyOfAnotherSchema(),
            FAILS_AS_BEFORE,
            id="other_model",
        ),
        pytest.param(
            list[SchemaWithRequiredValidationAlias],
            lambda: RootSchemaWithRequiredValidationAlias([SchemaWithRequiredValidationAlias.model_validate({"v": 1})]),
            FAILS_AS_BEFORE,
            id="root_model_for_list",
        ),
        # Subclasses can dump the fields of the response model to other values or add keys that it reads
        *(
            pytest.param(
                SchemaWithRequiredValidationAlias,
                lambda cls=cls, value=value: cls.model_validate({"v": value}),
                FAILS_AS_BEFORE,
                id=id,
            )
            for cls, value, id in [
                (SubclassWithFieldSerializer, 1, "subclass_with_field_serializer"),
                (SubclassWithModelSerializer, 1, "subclass_with_model_serializer"),
                (SubclassWithModelDumpOverride, 1, "subclass_with_model_dump_override"),
                (SubclassWithRetypedField, "text", "subclass_with_retyped_field"),
                (SubclassWithFieldOnTheDumpedKey, 1, "subclass_with_field_on_the_dumped_key"),
                (SubclassWithComputedFieldOnTheDumpedKey, 1, "subclass_with_computed_field_on_the_dumped_key"),
                (SubclassWithExtraAllow, 1, "subclass_with_extra_allow"),
            ]
        ),
        pytest.param(
            list[SchemaWithRequiredValidationAlias],
            lambda: [_subclass_instance(SubclassWithFieldSerializer)],
            FAILS_AS_BEFORE,
            id="list_of_subclass_with_field_serializer",
        ),
        pytest.param(
            RootSchemaWithRequiredValidationAlias,
            lambda: SubclassOfRootSchemaWithRequiredValidationAlias(
                [_subclass_instance(SchemaWithRequiredValidationAlias)]
            ),
            FAILS_AS_BEFORE,
            id="subclass_of_root_model",
        ),
        pytest.param(
            Annotated[list[SchemaWithRequiredValidationAlias], BeforeValidator(lambda value: value)],
            lambda: [SchemaWithRequiredValidationAlias.model_validate({"v": 1})],
            FAILS_AS_BEFORE,
            id="response_model_with_before_validator",
        ),
    ],
)
def test__response_migration__with_validation_aliases_in_dicts_other_models_or_unsafe_response_models__should_stay(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    response_model: Any,
    get_response: Callable[[], Any],
    expected: Any,
):
    @router.get(test_path, response_model=response_model)
    async def endpoint():
        return get_response()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        if expected is FAILS_AS_BEFORE:
            with pytest.raises(fastapi.exceptions.ResponseValidationError) as exc_info:
                version_client.get(test_path)
            assert {error["type"] for error in exc_info.value.errors()} == {"missing"}
        else:
            assert version_client.get(test_path).json() == expected


@pytest.mark.parametrize("fails_on_third_validation", [False, True], ids=["counts", "rejects_third_validation"])
def test__response_migration__with_validation_aliases__should_validate_the_response_as_many_times_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    fails_on_third_validation: bool,
):
    validations: list[str] = []

    class SchemaWithStatefulValidator(BaseModel):
        value: int = Field(default=0, alias="x", validation_alias="v")
        data: Any

        @model_validator(mode="after")
        def count_validations(self) -> Self:
            validations.append("validate")
            if fails_on_third_validation and len(validations) > 2:
                raise ValueError("Already validated twice")  # pragma: no cover # Only if the test fails
            self.data["count"] += 1
            return self

    @router.get(test_path, response_model=SchemaWithStatefulValidator)
    async def endpoint():
        return SchemaWithStatefulValidator.model_validate({"v": 7, "data": {"count": 0}})

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        validations.clear()
        # The endpoint validates the response once and FastAPI validates it once. It passes, so we don't touch it.
        assert version_client.get(test_path).json() == {"x": 0, "data": {"count": 2}}
        assert len(validations) == 2


class SchemaWithValidatorThatRaisesTypeError(BaseModel):
    value: int = Field(alias="x", validation_alias="v")

    @field_validator("value")
    @classmethod
    def check_value(cls, value: int) -> int:
        if value < 0:
            raise TypeError("Negative values cannot be processed")
        return value


def test__response_migration__with_rescue_that_raises__should_fail_with_the_original_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=SchemaWithValidatorThatRaisesTypeError)
    async def endpoint():
        return SchemaWithValidatorThatRaisesTypeError.model_validate({"v": 7})

    @convert_response_to_previous_version_for(SchemaWithValidatorThatRaisesTypeError)
    def migrator(response: ResponseInfo):
        response.body["x"] = -1

    clients = create_versioned_clients(version_change(migrator=migrator))

    # The restored response makes the validator raise, so the error is the one that the response had before
    with pytest.raises(fastapi.exceptions.ResponseValidationError) as exc_info:
        clients["2000-01-01"].get(test_path)
    assert [error["type"] for error in exc_info.value.errors()] == ["missing"]
    assert clients["2001-01-01"].get(test_path).json() == {"x": 7}


def test__response_migration__with_value_that_the_version_rejects__should_fail_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=InnerSchemaWithValidationAlias)
    async def endpoint():
        return _inner(5)

    clients = create_versioned_clients(version_change(schema(InnerSchemaWithValidationAlias).field("value").had(ge=10)))

    # The restored response fails too, so the error is the one that the response had before
    with pytest.raises(fastapi.exceptions.ResponseValidationError) as exc_info:
        clients["2000-01-01"].get(test_path)
    assert [error["type"] for error in exc_info.value.errors()] == ["missing"]
    assert clients["2001-01-01"].get(test_path).json() == {"x": 5}


class SchemaWithoutAliases(BaseModel):
    new_value: int


class SchemaWithOptionalInnerSchemaWithValidationAlias(BaseModel):
    inner: Optional[InnerSchemaWithValidationAlias] = None


class ValueThatCannotBeCompared:
    # Like numpy arrays, whose comparisons are element-wise. Cadwyn must never call it.
    @override
    def __eq__(self, other: object) -> bool:  # pragma: no cover
        raise ValueError("The truth value is ambiguous")

    __hash__ = None


class SchemaWithDefaultThatCannotBeCompared(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    value: int = Field(alias="x", validation_alias="v")
    other: ValueThatCannotBeCompared = Field(default=ValueThatCannotBeCompared(), exclude=True)


class SubclassWithAnotherDefaultThatCannotBeCompared(SchemaWithDefaultThatCannotBeCompared):
    other: ValueThatCannotBeCompared = Field(default=ValueThatCannotBeCompared(), exclude=True)


def test__response_migration__with_subclass_that_has_defaults_that_cannot_be_compared__client_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=SchemaWithDefaultThatCannotBeCompared)
    async def endpoint():
        return SubclassWithAnotherDefaultThatCannotBeCompared.model_validate({"v": 1})

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.get(test_path).json() == {"x": 1}


def test__response_migration__with_validation_aliases_and_response_model_exclude_unset__client_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(
        test_path,
        response_model=list[SchemaWithOptionalInnerSchemaWithValidationAlias],
        response_model_exclude_unset=True,
    )
    async def endpoint():
        return [
            SchemaWithOptionalInnerSchemaWithValidationAlias(inner=_inner(1)),
            SchemaWithOptionalInnerSchemaWithValidationAlias(),
        ]

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.get(test_path).json() == [{"inner": {"x": 1}}, {}]


def test__response_migration__with_validation_aliases_in_response_model_of_older_version__client_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=SchemaWithoutAliases)
    async def endpoint():
        return SchemaWithoutAliases(new_value=1)

    @convert_response_to_previous_version_for(test_path, ["GET"])
    def migrator(response: ResponseInfo):
        response.body = {"x": response.body["new_value"]}

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["GET"]).had(response_model=InnerSchemaWithValidationAlias), migrator=migrator
        )
    )

    assert clients["2000-01-01"].get(test_path).json() == {"x": 1}
    assert clients["2001-01-01"].get(test_path).json() == {"new_value": 1}


def test__response_migration__with_head_version_changes_and_validation_aliases__client_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=OuterSchemaWithValidationAliases)
    async def endpoint():
        return OuterSchemaWithValidationAliases.model_validate({"j": {"v": 1}, "items": [{"v": 2}]})

    clients = create_versioned_clients(
        version_change(),
        head_version_changes=[
            version_change(schema(OuterSchemaWithValidationAliases).field("mapping").had(description="Head"))
        ],
    )

    for version_client in clients.values():
        assert version_client.get(test_path).json() == {
            "i": {"x": 1},
            "items": [{"x": 2}],
            "mapping": {},
            "optional": None,
        }


@pytest.mark.parametrize(
    ("head_response_model", "head_response", "older_response_model", "older_response"),
    [
        pytest.param(
            Optional[SchemaWithRequiredValidationAlias],
            None,
            SchemaWithRequiredValidationAlias,
            {"x": 5},
            id="none_replaced_with_dict",
        ),
        pytest.param(list[SchemaWithRequiredValidationAlias], [], None, [{"x": 5}], id="empty_list_filled_with_dicts"),
    ],
)
def test__response_migration__that_adds_dicts_where_the_endpoint_returned_no_models__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    head_response_model: Any,
    head_response: Any,
    older_response_model: Any,
    older_response: Any,
):
    @router.get(test_path, response_model=head_response_model)
    async def endpoint():
        return head_response

    @convert_response_to_previous_version_for(test_path, ["GET"])
    def migrator(response: ResponseInfo):
        response.body = older_response

    instructions = (
        []
        if older_response_model is None
        else [cadwyn_endpoint(test_path, ["GET"]).had(response_model=older_response_model)]
    )
    clients = create_versioned_clients(version_change(*instructions, migrator=migrator))

    # The endpoint returned no models whose dumps we could restore, so the response fails as before
    with pytest.raises(fastapi.exceptions.ResponseValidationError) as exc_info:
        clients["2000-01-01"].get(test_path)
    assert {error["type"] for error in exc_info.value.errors()} == {"missing"}


@pytest.mark.parametrize(
    ("head_response_model", "get_response"),
    [
        pytest.param(AnyResponseSchema, lambda: [{"x": 1}], id="root_model_of_any"),
        pytest.param(
            None, lambda: [SchemaWithRequiredValidationAlias.model_validate({"v": 1})], id="no_head_response_model"
        ),
    ],
)
def test__response_migration__with_response_model_of_older_version_and_unknown_head_shape__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    head_response_model: Any,
    get_response: Callable[[], Any],
):
    @router.get(test_path, response_model=head_response_model)
    async def endpoint():
        return get_response()

    clients = create_versioned_clients(
        version_change(cadwyn_endpoint(test_path, ["GET"]).had(response_model=list[SchemaWithRequiredValidationAlias]))
    )

    with pytest.raises(fastapi.exceptions.ResponseValidationError) as exc_info:
        clients["2000-01-01"].get(test_path)
    assert {error["type"] for error in exc_info.value.errors()} == {"missing"}
    assert clients["2001-01-01"].get(test_path).json() == [{"x": 1}]


def _add_embedded_body_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: EmptySchema = Body(embed=True)):
        return body.model_dump()


def _add_annotated_embedded_body_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: Annotated[EmptySchema, Body(embed=True)]):
        return body.model_dump()


def _add_embedded_body_with_validation_aliases_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: InnerSchemaWithValidationAlias = Body(embed=True)):
        return body.value


def _add_embedded_scalar_body_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: int = Body(embed=True)):
        return body


def _add_single_form_field_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: str = Form()):
        return body


def _add_embedded_body_with_alias_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: InnerSchemaWithValidationAlias = Body(alias="payload", embed=True)):
        return body.value


def _add_optional_embedded_body_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: Optional[InnerSchemaWithValidationAlias] = Body(None, embed=True)):
        return body.value if body is not None else None


def _add_optional_embedded_body_next_to_optional_query_param_named_body_endpoint(
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(
        payload: Optional[InnerSchemaWithValidationAlias] = Body(None, embed=True), body: Optional[str] = Query(None)
    ):
        return [payload.value if payload is not None else None, body]


def _add_embedded_body_next_to_query_param_named_body_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(payload: InnerSchemaWithValidationAlias = Body(embed=True), body: str = Query()):
        return [payload.value, body]


async def _dependency_with_body_param(count: int = Body()) -> int:
    return count


def _add_body_next_to_body_of_dependency_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: InnerSchemaWithValidationAlias, count: int = Depends(_dependency_with_body_param)):
        return [body.value, count]


def _add_body_with_alias_of_query_param_endpoint(router: VersionedAPIRouter, path: str) -> None:
    @router.post(path)
    async def endpoint(body: InnerSchemaWithValidationAlias = Body(alias="payload"), payload: str = Query()):
        return [body.value, payload]


@pytest.mark.parametrize(
    ("add_endpoint", "request_kwargs", "expected"),
    [
        pytest.param(_add_embedded_body_endpoint, {"json": {"body": {}}}, {}, id="embedded_body"),
        pytest.param(_add_annotated_embedded_body_endpoint, {"json": {"body": {}}}, {}, id="annotated_embedded_body"),
        pytest.param(
            _add_embedded_body_with_validation_aliases_endpoint,
            {"json": {"body": {"v": 1}}},
            1,
            id="embedded_body_with_validation_aliases",
        ),
        pytest.param(_add_embedded_scalar_body_endpoint, {"json": {"body": 1}}, 1, id="embedded_scalar_body"),
        pytest.param(_add_single_form_field_endpoint, {"data": {"body": "1"}}, "1", id="single_form_field"),
        pytest.param(
            _add_embedded_body_with_alias_endpoint,
            {"json": {"payload": {"v": 1}}},
            1,
            id="embedded_body_with_alias",
        ),
        pytest.param(
            _add_optional_embedded_body_endpoint,
            {"json": {"body": {"v": 1}}},
            1,
            id="optional_embedded_body",
        ),
        pytest.param(
            _add_optional_embedded_body_next_to_optional_query_param_named_body_endpoint,
            {"json": {"payload": {"v": 1}}, "params": {"body": "q"}},
            [1, "q"],
            id="optional_embedded_body_next_to_optional_query_param_named_body",
        ),
        pytest.param(
            _add_embedded_body_next_to_query_param_named_body_endpoint,
            {"json": {"payload": {"v": 1}}, "params": {"body": "q"}},
            [1, "q"],
            id="embedded_body_next_to_query_param_named_body",
        ),
        pytest.param(
            _add_body_next_to_body_of_dependency_endpoint,
            {"json": {"body": {"v": 1}, "count": 2}},
            [1, 2],
            id="body_next_to_body_of_dependency",
        ),
        pytest.param(
            _add_body_with_alias_of_query_param_endpoint,
            {"json": {"v": 1}, "params": {"payload": "q"}},
            [1, "q"],
            id="body_with_alias_of_query_param",
        ),
    ],
)
def test__request_body_migration__with_embedded_body_or_body_alias__head_should_get_the_body(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    add_endpoint: Callable[[VersionedAPIRouter, str], None],
    request_kwargs: dict[str, Any],
    expected: Any,
):
    add_endpoint(router, test_path)
    bodies_seen_by_migration = []

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        bodies_seen_by_migration.append(request.body)

    clients = create_versioned_clients(version_change(migrator=migrator))

    for version_client in clients.values():
        assert version_client.post(test_path, **request_kwargs).json() == expected
    # Migrations see the request body exactly as the client sent it
    raw_body = request_kwargs.get("json", request_kwargs.get("data"))
    assert [dict(body) for body in bodies_seen_by_migration] == [raw_body]


class EmbeddedSchema(BaseModel):
    value: int


def test__request_body_migration__with_embedded_body_changed_in_old_version__head_should_get_the_migrated_body(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: EmbeddedSchema = Body(embed=True)):
        return body.value

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body["body"]["value"] = request.body["body"].pop("old_value")

    clients = create_versioned_clients(
        version_change(schema(EmbeddedSchema).field("value").had(name="old_value"), migrator=migrator)
    )

    assert clients["2000-01-01"].post(test_path, json={"body": {"old_value": 1}}).json() == 1
    assert clients["2001-01-01"].post(test_path, json={"body": {"value": 1}}).json() == 1
    for version, field_name in [("2000-01-01", "old_value"), ("2001-01-01", "value")]:
        openapi = clients[version].get(f"/openapi.json?version={version}").json()
        body_ref = openapi["paths"][test_path]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        body_schema = openapi["components"]["schemas"][body_ref.removeprefix("#/components/schemas/")]
        assert body_schema["required"] == ["body"]
        embedded_ref = body_schema["properties"]["body"]["$ref"]
        embedded_schema = openapi["components"]["schemas"][embedded_ref.removeprefix("#/components/schemas/")]
        assert list(embedded_schema["properties"]) == [field_name]


class SchemaWithDifferentAliases(BaseModel):
    value: int = Field(alias="out", validation_alias="in")


def _get_checked_body(body: SchemaWithDifferentAliases) -> SchemaWithDifferentAliases:
    return body


@pytest.mark.parametrize("migration_target", [(SchemaWithDifferentAliases,), ("/test", ["POST"])])
def test__request_body_migration__with_body_alias_of_dependency_returning_the_body__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    migration_target: tuple[Any, ...],
):
    @router.post(test_path)
    async def endpoint(
        body: SchemaWithDifferentAliases = Body(alias="checked"),
        checked: SchemaWithDifferentAliases = Depends(_get_checked_body),
    ):
        return body.value

    bodies_seen_by_migration = []

    @convert_request_to_next_version_for(*migration_target)
    def migrator(request: RequestInfo):
        bodies_seen_by_migration.append(dict(request.body))
        request.body["out"] = 7

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json={"in": 4}).json() == 7
    assert clients["2001-01-01"].post(test_path, json={"in": 4}).json() == 4
    assert bodies_seen_by_migration == [{"out": 4}]


@pytest.mark.parametrize("annotation", [bytes, list[bytes]])
def test__request_body_migration__with_file_named_body__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
):
    @router.post(test_path)
    async def endpoint(body: annotation = File()):
        raise NotImplementedError

    clients = create_versioned_clients(version_change())

    # FastAPI has already read the uploaded files so reading the request again would deliver them empty
    for version_client in clients.values():
        with pytest.raises(CadwynHeadRequestValidationError):
            version_client.post(test_path, files={"body": ("file.txt", b"content")})


async def _bytes_upload_endpoint(upload: bytes = File()):
    return [upload.decode()]


async def _annotated_bytes_upload_endpoint(upload: Annotated[bytes, File()]):
    return [upload.decode()]


async def _optional_bytes_upload_endpoint(upload: Optional[bytes] = File(None)):
    return [upload.decode() if upload is not None else None]


async def _bytes_list_upload_endpoint(upload: list[bytes] = File()):
    return [file.decode() for file in upload]


async def _bytes_upload_with_form_endpoint(upload: bytes = File(), name: str = Form()):
    return [upload.decode(), name]


async def _bytes_upload_with_form_named_body_endpoint(upload: bytes = File(), body: str = Form()):
    return [upload.decode(), body]


async def _bytes_and_upload_file_endpoint(upload: bytes = File(), other: UploadFile = File()):
    return [upload.decode(), (await other.read()).decode()]


def _sync_bytes_and_upload_file_endpoint(upload: bytes = File(), other: UploadFile = File()):
    return [upload.decode(), other.file.read().decode()]


async def _upload_file_endpoint(upload: UploadFile = File()):
    return [(await upload.read()).decode()]


async def _upload_file_list_endpoint(upload: list[UploadFile] = File()):
    return [(await file.read()).decode() for file in upload]


@pytest.mark.parametrize(
    ("endpoint", "request_kwargs", "expected"),
    [
        pytest.param(_bytes_upload_endpoint, {"files": {"upload": b"first"}}, ["first"], id="bytes"),
        pytest.param(_bytes_upload_endpoint, {"data": {"upload": "text"}}, ["text"], id="bytes_sent_as_text"),
        pytest.param(_annotated_bytes_upload_endpoint, {"files": {"upload": b"first"}}, ["first"], id="annotated"),
        pytest.param(_optional_bytes_upload_endpoint, {"files": {"upload": b"first"}}, ["first"], id="optional"),
        pytest.param(_optional_bytes_upload_endpoint, {"data": {"other": "text"}}, [None], id="optional_missing"),
        pytest.param(
            _bytes_list_upload_endpoint,
            {"files": [("upload", b"first"), ("upload", b"second")]},
            ["first", "second"],
            id="list_of_bytes",
        ),
        pytest.param(
            _bytes_upload_with_form_endpoint,
            {"files": {"upload": b"first"}, "data": {"name": "my_name"}},
            ["first", "my_name"],
            id="bytes_with_form",
        ),
        pytest.param(
            _bytes_upload_with_form_named_body_endpoint,
            {"files": {"upload": b"first"}, "data": {"body": "form"}},
            ["first", "form"],
            id="bytes_with_form_named_body",
        ),
        pytest.param(
            _bytes_and_upload_file_endpoint,
            {"files": [("upload", b"first"), ("other", b"second")]},
            ["first", "second"],
            id="bytes_with_upload_file",
        ),
        pytest.param(
            _sync_bytes_and_upload_file_endpoint,
            {"files": [("upload", b"first"), ("other", b"second")]},
            ["first", "second"],
            id="sync_bytes_with_upload_file",
        ),
        pytest.param(_upload_file_endpoint, {"files": {"upload": b"first"}}, ["first"], id="upload_file"),
        pytest.param(
            _upload_file_list_endpoint,
            {"files": [("upload", b"first"), ("upload", b"second")]},
            ["first", "second"],
            id="list_of_upload_files",
        ),
    ],
)
def test__request_body_migration__with_file_uploads__head_should_get_the_uploaded_contents(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Any],
    request_kwargs: dict[str, Any],
    expected: list[Optional[str]],
):
    router.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, **request_kwargs).json() == expected


def test__form_migration__can_modify_form_with_bytes_uploads(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File(), replaced: bytes = File(), name: str = Form()):
        return [upload.decode(), replaced.decode(), name]

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        replaced_file = UploadFile(BytesIO(b"migrated"), filename="replaced")
        request.form[:] = [
            (key, "migrated_name" if key == "name" else replaced_file if key == "replaced" else value)
            for key, value in request.form
        ]

    clients = create_versioned_clients(version_change(migrator=migrator))
    files = [("upload", b"first"), ("replaced", b"second")]

    assert clients["2000-01-01"].post(test_path, files=files, data={"name": "my_name"}).json() == [
        "first",
        "migrated",
        "migrated_name",
    ]
    assert clients["2001-01-01"].post(test_path, files=files, data={"name": "my_name"}).json() == [
        "first",
        "second",
        "my_name",
    ]


@pytest.mark.parametrize("annotation", [bytes, UploadFile])
def test__request_body_migration__with_file_uploads_rolled_to_disk__head_should_get_the_uploaded_contents(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
):
    # Bigger than what starlette keeps in memory so that the uploaded file gets rolled to disk
    sent_contents = bytes(range(256)) * (MultiPartParser.spool_max_size // 256 + 1)
    assert len(sent_contents) > MultiPartParser.spool_max_size

    @router.post(test_path)
    async def endpoint(upload: annotation = File()):
        contents = upload if isinstance(upload, bytes) else await upload.read()
        return {"size": len(contents), "is_same": contents == sent_contents}

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": sent_contents}).json() == {
            "size": len(sent_contents),
            "is_same": True,
        }


async def _get_data_from_bytes(data: bytes = File()) -> str:
    return data.decode()


async def _get_data_from_bytes_list(data: list[bytes] = File()) -> list[str]:
    return [file.decode() for file in data]


async def _get_data_from_sub_dependency(data: str = Depends(_get_data_from_bytes)) -> str:
    return data


async def _get_data_from_uncached_sub_dependency(data: str = Depends(_get_data_from_bytes, use_cache=False)) -> str:
    return data


async def _bytes_dependency_endpoint(data: str = Depends(_get_data_from_bytes)):
    return [data]


async def _bytes_list_dependency_endpoint(data: list[str] = Depends(_get_data_from_bytes_list)):
    return data


async def _nested_bytes_dependency_endpoint(data: str = Depends(_get_data_from_sub_dependency)):
    return [data]


async def _bytes_dependency_with_form_endpoint(data: str = Depends(_get_data_from_bytes), name: str = Form()):
    return [data, name]


async def _cached_bytes_dependency_endpoint(
    data: str = Depends(_get_data_from_bytes), nested_data: str = Depends(_get_data_from_sub_dependency)
):
    return [data, nested_data]


async def _uncached_bytes_dependency_endpoint(
    data: str = Depends(_get_data_from_bytes), nested_data: str = Depends(_get_data_from_uncached_sub_dependency)
):
    return [data, nested_data]


async def _bytes_in_endpoint_and_dependency_endpoint(
    data: bytes = File(), from_dependency: str = Depends(_get_data_from_bytes)
):
    return [data.decode(), from_dependency]


@pytest.mark.parametrize(
    ("endpoint", "request_kwargs", "expected"),
    [
        pytest.param(_bytes_dependency_endpoint, {"files": {"data": b"first"}}, ["first"], id="bytes"),
        pytest.param(
            _bytes_list_dependency_endpoint,
            {"files": [("data", b"first"), ("data", b"second")]},
            ["first", "second"],
            id="list_of_bytes",
        ),
        pytest.param(_nested_bytes_dependency_endpoint, {"files": {"data": b"first"}}, ["first"], id="nested"),
        pytest.param(
            _bytes_dependency_with_form_endpoint,
            {"files": {"data": b"first"}, "data": {"name": "my_name"}},
            ["first", "my_name"],
            id="bytes_with_form",
        ),
        # Same as in FastAPI itself: the files get read once, and the cache gives the dependency's result again
        pytest.param(_cached_bytes_dependency_endpoint, {"files": {"data": b"first"}}, ["first", "first"], id="cached"),
        # Same as in FastAPI itself: before FastAPI 0.157.0, only the first param that reads the file gets its contents
        pytest.param(
            _uncached_bytes_dependency_endpoint,
            {"files": {"data": b"first"}},
            ["first", "first" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ""],
            id="uncached",
        ),
        pytest.param(
            _bytes_in_endpoint_and_dependency_endpoint,
            {"files": {"data": b"first"}},
            ["first" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else "", "first"],
            id="in_endpoint_and_dependency",
        ),
    ],
)
def test__request_body_migration__with_file_uploads_in_dependencies__head_should_get_the_uploaded_contents(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Any],
    request_kwargs: dict[str, Any],
    expected: list[str],
):
    router.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, **request_kwargs).json() == expected


async def _get_data_header(data: UploadFile = File()) -> str:
    return (await data.read(2)).decode()


async def _get_other_from_bytes(other: bytes = File()) -> str:
    return other.decode()


def test__request_body_migration__with_bytes_dependency_overridden__should_keep_the_override_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(data: str = Depends(_get_data_from_bytes), other: str = Depends(_get_other_from_bytes)):
        return [data, other]

    clients = create_versioned_clients(version_change())
    app = clients["2000-01-01"].app
    app.dependency_overrides[_get_data_from_bytes] = _get_data_header

    # The override reads a part of the file for every route as before, while the files of other dependencies
    # don't arrive empty anymore
    for version_client in clients.values():
        assert version_client.post(test_path, files={"data": b"DATA", "other": b"second"}).json() == ["TA", "second"]


async def _get_user() -> str:
    raise NotImplementedError


@dataclasses.dataclass
class _UnhashableUserOverride:
    name: str

    async def __call__(self) -> str:
        return self.name


def test__request_body_migration__with_bytes_dependency_and_unhashable_override__head_should_get_the_contents(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(user: str = Depends(_get_user), data: str = Depends(_get_data_from_bytes)):
        return [user, data]

    clients = create_versioned_clients(version_change())
    app = clients["2000-01-01"].app
    # Dataclasses that compare by value aren't hashable
    app.dependency_overrides[_get_user] = _UnhashableUserOverride("alice")

    for version_client in clients.values():
        assert version_client.post(test_path, files={"data": b"first"}).json() == ["alice", "first"]


async def _read_upload_as_bytes_in_old_version(upload: bytes = File()) -> None:
    assert upload == b"first"


@pytest.mark.parametrize(
    ("annotation", "expected_in_old_version"),
    [(bytes, "first"), (UploadFile, "first" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else "")],
)
def test__request_body_migration__with_bytes_read_by_old_version_dependency__head_should_get_them_only_as_bytes(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
    expected_in_old_version: str,
):
    @router.post(test_path)
    async def endpoint(upload: annotation = File()):
        return await _read_upload_as_bytes(upload)

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[Depends(_read_upload_as_bytes_in_old_version)])
        )
    )

    # The head has no dependency that takes the file as bytes, so the `UploadFile` stays where the old one left it
    assert clients["2000-01-01"].post(test_path, files={"upload": b"first"}).json() == expected_in_old_version
    assert clients["2001-01-01"].post(test_path, files={"upload": b"first"}).json() == "first"


async def _get_last_file(last: bytes = File(alias="f")) -> str:
    return last.decode()


def test__request_body_migration__with_bytes_alias_shared_by_dependency_and_endpoint_list__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(files: list[bytes] = File(alias="f"), last: str = Depends(_get_last_file)):
        return [last, [file.decode() for file in files]]

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        request.form.append(("f", UploadFile(BytesIO(b"NEW"), filename="new")))

    clients = create_versioned_clients(version_change(migrator=migrator))
    files = [("f", b"A"), ("f", b"B")]

    # The dependency reads the last file first, so the endpoint gets it empty before FastAPI 0.157.0, as in FastAPI
    assert clients["2000-01-01"].post(test_path, files=files).json() == [
        "NEW",
        ["A", "B", "NEW" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ""],
    ]
    assert clients["2001-01-01"].post(test_path, files=files).json() == [
        "B",
        ["A", "B" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ""],
    ]


async def _read_and_close_upload(request: Request, upload: bytes = File()) -> str:
    form_upload = (await request.form())["upload"]
    assert isinstance(form_upload, StarletteUploadFile)
    await form_upload.close()
    return upload.decode()


def test__form_migration__with_bytes_upload_closed_by_dependency_and_replaced__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(contents: str = Depends(_read_and_close_upload)):
        return contents

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        request.form[:] = [("upload", UploadFile(BytesIO(b"normalized"), filename="normalized"))]

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, files={"upload": b"original"}).json() == "normalized"


async def _store_upload(request: Request, upload: bytes = File()) -> None:
    request.state.contents = upload.decode()


async def _swap_in_stream_after_legacy_header(upload: UploadFile = File()) -> None:
    old_stream = upload.file
    # The form closes the new stream like the one it replaces
    upload.file = cast("Any", tempfile.SpooledTemporaryFile())  # noqa: SIM115
    upload.file.write(b"xxNORMALIZED")
    upload.file.seek(len(b"xx"))
    old_stream.close()


async def _swap_in_stream_left_at_its_end(upload: UploadFile = File()) -> None:
    old_stream = upload.file
    # The form closes the new stream like the one it replaces
    upload.file = cast("Any", tempfile.SpooledTemporaryFile())  # noqa: SIM115
    upload.file.write(b"NORMALIZED")
    old_stream.close()


async def _skip_legacy_header_after_reading(upload: UploadFile = File()) -> None:
    await upload.seek(len(b"or"))


async def _skip_legacy_header_on_disk_after_reading(upload: UploadFile = File()) -> None:
    cast("tempfile.SpooledTemporaryFile[bytes]", upload.file).rollover()
    await upload.seek(len(b"or"))


@pytest.mark.parametrize(
    ("legacy_dependency", "sent", "expected"),
    [
        (_swap_in_stream_after_legacy_header, b"original", "NORMALIZED"),
        (_swap_in_stream_left_at_its_end, b"original", ""),
        (_skip_legacy_header_after_reading, b"original", "iginal"),
        # A header without a payload leaves the file at its end, like reading it does
        (_skip_legacy_header_after_reading, b"or", ""),
        (_skip_legacy_header_on_disk_after_reading, b"or", ""),
    ],
)
def test__request_body_migration__with_bytes_moved_by_old_version_dependency__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    legacy_dependency: Callable[..., Coroutine[Any, Any, None]],
    sent: bytes,
    expected: str,
):
    @router.post(test_path, dependencies=[Depends(_store_upload)])
    async def endpoint(request: Request):
        return request.state.contents

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[Depends(_store_upload), Depends(legacy_dependency)])
        )
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": sent}).json() == expected
    assert clients["2001-01-01"].post(test_path, files={"upload": sent}).json() == sent.decode()


async def _get_upload(upload: UploadFile = File()) -> UploadFile:
    return upload


async def _normalize_upload_from_request_form(request: Request) -> None:
    upload = (await request.form())["upload"]
    assert isinstance(upload, StarletteUploadFile)
    upload.file.seek(0)
    upload.file.truncate()
    upload.file.write(b"NORMALIZED")


async def _normalize_upload_from_shared_dependency(upload: UploadFile = Depends(_get_upload)) -> None:
    upload.file.seek(0)
    upload.file.truncate()
    upload.file.write(b"NORMALIZED")


async def _swap_upload_from_shared_dependency(upload: UploadFile = Depends(_get_upload)) -> None:
    old_stream = upload.file
    # The form closes the new stream like the one it replaces
    upload.file = cast("Any", tempfile.SpooledTemporaryFile())  # noqa: SIM115
    upload.file.write(b"NORMALIZED")
    old_stream.close()


@pytest.mark.parametrize(
    "legacy_dependency",
    [
        _normalize_upload_from_request_form,
        _normalize_upload_from_shared_dependency,
        _swap_upload_from_shared_dependency,
    ],
)
def test__request_body_migration__with_bytes_changed_by_old_version_dependency_indirectly__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    legacy_dependency: Callable[..., Coroutine[Any, Any, None]],
):
    @router.post(test_path, dependencies=[Depends(_store_upload)])
    async def endpoint(request: Request, upload: UploadFile = Depends(_get_upload)):
        return request.state.contents

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[Depends(_store_upload), Depends(legacy_dependency)])
        )
    )

    # The dependency left the file at its end without declaring it, so the files of dependencies stay as before
    assert clients["2000-01-01"].post(test_path, files={"upload": b"original"}).json() == ""
    assert clients["2001-01-01"].post(test_path, files={"upload": b"original"}).json() == "original"


async def _prepare_upload(security_scopes: SecurityScopes, upload: UploadFile = File()) -> None:
    if "strip_header" in security_scopes.scopes:
        await upload.seek(len(b"or"))


def test__request_body_migration__with_bytes_and_old_version_security_scopes__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, dependencies=[Depends(_store_upload), Security(_prepare_upload, scopes=["check"])])
    async def endpoint(request: Request):
        return request.state.contents

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(
                dependencies=[
                    Depends(_store_upload),
                    Security(_prepare_upload, scopes=["check"]),
                    Security(_prepare_upload, scopes=["strip_header"]),
                ]
            )
        )
    )

    # Only the old version runs the dependency with these scopes, and it leaves a header without a payload at its end
    assert clients["2000-01-01"].post(test_path, files={"upload": b"or"}).json() == ""
    assert clients["2001-01-01"].post(test_path, files={"upload": b"or"}).json() == "or"


async def _check_upload() -> None:
    raise NotImplementedError


async def _strip_legacy_header() -> None:
    raise NotImplementedError


async def _prepare_upload_in_second_run(request: Request, upload: UploadFile = File()) -> None:
    request.state.runs = getattr(request.state, "runs", 0) + 1
    if request.state.runs == 2:
        await upload.seek(len(b"or"))


class _EqualDependency:
    """Instances are distinct dependencies that compare equal."""

    @override
    def __hash__(self) -> int:
        return 0

    @override
    def __eq__(self, other: object) -> bool:
        return True if isinstance(other, _EqualDependency) else NotImplemented

    async def __call__(self, upload: UploadFile = File()) -> None:
        pass


_shared_dependency = _EqualDependency()


@pytest.mark.parametrize(
    ("head_dependency", "added_dependency", "dependency_overrides", "expected_in_old_version"),
    [
        pytest.param(
            Depends(_check_upload),
            Depends(_strip_legacy_header),
            {_check_upload: _prepare_upload_in_second_run, _strip_legacy_header: _prepare_upload_in_second_run},
            "",
            id="same_override",
        ),
        # Only FastAPI before 0.157.0 leaves the file at its end here
        pytest.param(
            Depends(_prepare_upload_in_second_run),
            Depends(_prepare_upload_in_second_run, use_cache=False),
            {},
            "or" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else "",
            id="uncached_copy",
        ),
        pytest.param(
            Depends(_shared_dependency, use_cache=False),
            Depends(_EqualDependency(), use_cache=False),
            {},
            "or" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else "",
            id="equal_callable",
        ),
    ],
)
def test__request_body_migration__with_bytes_and_old_version_dependency_like_a_head_one__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    head_dependency: Any,
    added_dependency: Any,
    dependency_overrides: dict[Any, Any],
    expected_in_old_version: str,
):
    @router.post(test_path, dependencies=[Depends(_store_upload), head_dependency])
    async def endpoint(request: Request):
        return request.state.contents

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(
                dependencies=[Depends(_store_upload), head_dependency, added_dependency]
            )
        )
    )
    app = clients["2000-01-01"].app
    app.dependency_overrides.update(dependency_overrides)

    # The old version runs one more dependency than the head, even though it looks like the head's one
    assert clients["2000-01-01"].post(test_path, files={"upload": b"or"}).json() == expected_in_old_version
    assert clients["2001-01-01"].post(test_path, files={"upload": b"or"}).json() == "or"


async def _skip_nothing(upload: UploadFile = File()) -> None:
    raise NotImplementedError


def test__request_body_migration__with_bytes_moved_by_dependency_of_every_version__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, dependencies=[Depends(_store_upload), Depends(_skip_legacy_header_after_reading)])
    async def endpoint(request: Request):
        return request.state.contents

    clients = create_versioned_clients(version_change())

    # The file isn't where FastAPI's reading left it, so we keep it where the dependency moved it
    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": b"original"}).json() == "iginal"


async def _skip_legacy_header_in_override(upload: UploadFile = File()) -> None:
    await upload.seek(len(b"or"))


def test__request_body_migration__with_bytes_and_overridden_old_version_dependency__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, dependencies=[Depends(_store_upload)])
    async def endpoint(request: Request):
        return request.state.contents

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[Depends(_store_upload), Depends(_skip_nothing)])
        )
    )
    app = clients["2000-01-01"].app
    app.dependency_overrides[_skip_nothing] = _skip_legacy_header_in_override

    # We can't see which files the override takes, so the files of dependencies stay as before. Here the override
    # skips a header without a payload, which leaves the file at its end like reading it does
    assert clients["2000-01-01"].post(test_path, files={"upload": b"or"}).json() == ""
    assert clients["2001-01-01"].post(test_path, files={"upload": b"or"}).json() == "or"


async def _remove_legacy_trailer(upload: UploadFile = File()) -> None:
    contents = await upload.read()
    # Leaves the position after the new end
    upload.file.truncate(len(contents) - len(b"\r\n"))


def test__request_body_migration__with_endpoint_bytes_truncated_by_old_version_dependency__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File()):
        return upload.decode()

    clients = create_versioned_clients(
        version_change(cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[Depends(_remove_legacy_trailer)]))
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": b"NORMALIZED\r\n"}).json() == "NORMALIZED"
    assert clients["2001-01-01"].post(test_path, files={"upload": b"NORMALIZED\r\n"}).json() == "NORMALIZED\r\n"


async def _read_upload_as_bytes(upload: Union[bytes, UploadFile]) -> str:
    return (upload if isinstance(upload, bytes) else await upload.read()).decode()


class _ReadOnlyStream(RawIOBase):
    def __init__(self, contents: bytes) -> None:
        self.remaining = contents

    @override
    def read(self, size: int = -1) -> bytes:
        count = len(self.remaining) if size < 0 else size
        result, self.remaining = self.remaining[:count], self.remaining[count:]
        return result


def _skip_legacy_header(request: RequestInfo):
    assert request.form is not None
    for _, value in request.form:
        assert isinstance(value, StarletteUploadFile)
        value.file.seek(len(b"HDR:"))


def _replace_with_skipped_header(request: RequestInfo):
    assert request.form is not None
    replacement = UploadFile(BytesIO(b"HDR:payload"), filename="replacement")
    replacement.file.seek(len(b"HDR:"))
    request.form[:] = [("upload", replacement)]


def _replace_with_non_seekable_stream(request: RequestInfo):
    assert request.form is not None
    request.form[:] = [("upload", UploadFile(cast("Any", _ReadOnlyStream(b"payload")), filename="replacement"))]


@pytest.mark.parametrize(
    ("annotation", "migration"),
    [
        (bytes, _skip_legacy_header),
        (UploadFile, _skip_legacy_header),
        (bytes, _replace_with_skipped_header),
        (UploadFile, _replace_with_skipped_header),
        # Non-seekable streams for `bytes` params are tested separately because FastAPI 0.157.0 stopped supporting them
        (UploadFile, _replace_with_non_seekable_stream),
    ],
)
def test__form_migration__with_file_positions_set_by_migration__head_should_keep_them(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
    migration: Callable[[RequestInfo], None],
):
    @router.post(test_path)
    async def endpoint(upload: annotation = File()):
        return await _read_upload_as_bytes(upload)

    clients = create_versioned_clients(
        version_change(migrator=convert_request_to_next_version_for(test_path, ["POST"])(migration))
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": b"HDR:payload"}).json() == "payload"
    assert clients["2001-01-01"].post(test_path, files={"upload": b"HDR:payload"}).json() == "HDR:payload"


def _swap_stream_for_non_seekable_one(request: RequestInfo):
    assert request.form is not None
    for _, value in request.form:
        assert isinstance(value, StarletteUploadFile)
        value.file.close()
        value.file = cast("Any", _ReadOnlyStream(b"decoded"))


def _swap_stream_for_one_at_the_same_position(request: RequestInfo):
    assert request.form is not None
    for _, value in request.form:
        assert isinstance(value, StarletteUploadFile)
        value.file.close()
        # The position where FastAPI left the original file
        value.file = BytesIO(b"NEWDATA")
        value.file.seek(len(b"12345"))


def test__form_migration__with_bytes_upload_stream_swapped_by_migration__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File()):
        return upload.decode()

    clients = create_versioned_clients(
        version_change(
            migrator=convert_request_to_next_version_for(test_path, ["POST"])(_swap_stream_for_one_at_the_same_position)
        )
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": b"12345"}).json() == "TA"


@pytest.mark.skipif(
    FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES, reason="Since FastAPI 0.157.0, `bytes` params require seekable files"
)
@pytest.mark.parametrize(
    ("migration", "expected"),
    [(_replace_with_non_seekable_stream, "payload"), (_swap_stream_for_non_seekable_one, "decoded")],
)
def test__form_migration__with_non_seekable_stream_for_bytes_param__head_should_read_it_as_is(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    migration: Callable[[RequestInfo], None],
    expected: str,
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File()):
        return upload.decode()

    clients = create_versioned_clients(
        version_change(migrator=convert_request_to_next_version_for(test_path, ["POST"])(migration))
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": b"HDR:payload"}).json() == expected


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.157
    not FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES, reason="Before FastAPI 0.157.0, `bytes` params take any stream"
)
@pytest.mark.parametrize("migration", [_replace_with_non_seekable_stream, _swap_stream_for_non_seekable_one])
def test__form_migration__with_non_seekable_stream_for_bytes_param__should_raise_unsupported_operation_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    migration: Callable[[RequestInfo], None],
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File()):
        raise NotImplementedError

    clients = create_versioned_clients(
        version_change(migrator=convert_request_to_next_version_for(test_path, ["POST"])(migration))
    )

    # Cadwyn passes the stream on as is, and FastAPI can't put a non-seekable stream back where it was after reading it
    with pytest.raises(UnsupportedOperation):
        clients["2000-01-01"].post(test_path, files={"upload": b"HDR:payload"})


def _move_upload_to_attachment(request: RequestInfo):
    assert request.form is not None
    request.form[:] = [("upload", "converted"), ("attachment", dict(request.form)["upload"])]


def _shadow_upload_and_reuse_it_as_attachment(request: RequestInfo):
    assert request.form is not None
    original = dict(request.form)["upload"]
    request.form.extend([("upload", "converted"), ("attachment", original)])


def _reuse_upload_as_attachment(request: RequestInfo):
    assert request.form is not None
    request.form.append(("attachment", dict(request.form)["upload"]))


@pytest.mark.parametrize(
    ("migration", "expected"),
    [
        # Before FastAPI 0.157.0, FastAPI leaves the file empty after reading it as `bytes` for the versioned route
        (_move_upload_to_attachment, ["converted", "legacy" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ""]),
        (
            _shadow_upload_and_reuse_it_as_attachment,
            ["converted", "legacy" if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ""],
        ),
        (_reuse_upload_as_attachment, ["legacy", "legacy"] if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ["", ""]),
    ],
)
def test__form_migration__with_bytes_upload_moved_to_another_field__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    migration: Callable[[RequestInfo], None],
    expected: list[str],
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File(), attachment: Optional[UploadFile] = File(None)):
        assert attachment is not None
        return [upload.decode(), (await attachment.read()).decode()]

    clients = create_versioned_clients(
        version_change(migrator=convert_request_to_next_version_for(test_path, ["POST"])(migration))
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": b"legacy"}).json() == expected


def test__form_migration__with_bytes_upload_stream_shared_with_another_upload__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(attachment: bytes = File(), upload: bytes = File()):
        return [attachment.decode(), upload.decode()]

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        fields = dict(request.form)
        assert isinstance(fields["attachment"], StarletteUploadFile)
        assert isinstance(fields["upload"], StarletteUploadFile)
        fields["attachment"].file.close()
        fields["attachment"].file = fields["upload"].file

    clients = create_versioned_clients(version_change(migrator=migrator))
    files = [("attachment", b"old"), ("upload", b"legacy")]

    # Before FastAPI 0.157.0, FastAPI leaves the file empty after reading it as `bytes` for the versioned route
    assert clients["2000-01-01"].post(test_path, files=files).json() == (
        ["legacy", "legacy"] if FASTAPI_KEEPS_POSITIONS_OF_BYTES_FILES else ["", ""]
    )


def test__form_migration__with_bytes_upload_closed_by_migration__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: bytes = File()):
        raise NotImplementedError

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        assert request.form is not None
        for _, value in request.form:
            assert isinstance(value, StarletteUploadFile)
            value.file.close()

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(ValueError, match="closed file"):
        clients["2000-01-01"].post(test_path, files={"upload": b"payload"})


async def _skip_legacy_header_in_dependency(upload: UploadFile = File()) -> None:
    await upload.read(len(b"HDR:"))


def test__request_body_migration__with_upload_file_read_by_old_version_dependency__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: UploadFile = File()):
        return (await upload.read()).decode()

    clients = create_versioned_clients(
        version_change(
            cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[Depends(_skip_legacy_header_in_dependency)])
        )
    )

    assert clients["2000-01-01"].post(test_path, files={"upload": b"HDR:payload"}).json() == "payload"
    assert clients["2001-01-01"].post(test_path, files={"upload": b"HDR:payload"}).json() == "HDR:payload"


async def _read_header_once(request: Request, upload: UploadFile = File()) -> str:
    if not hasattr(request.state, "header"):
        request.state.header = (await upload.read(2)).decode()
    return request.state.header


def test__request_body_migration__with_upload_file_read_by_cached_dependency__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: UploadFile = File(), header: str = Depends(_read_header_once)):
        return [header, (await upload.read()).decode()]

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": b"HEADERdata"}).json() == ["HE", "ADERdata"]


async def _replace_upload(upload: UploadFile = File()) -> dict[str, UploadFile]:
    return {"upload": UploadFile(BytesIO(b"normalized"), filename=upload.filename)}


def test__request_body_migration__with_upload_replaced_by_dependency_named_body__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(upload: UploadFile = File(), body: Any = Depends(_replace_upload)):
        return (await upload.read()).decode()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": b"original"}).json() == "normalized"


async def _parse_form(request: Request) -> dict[str, Any]:
    return dict(await request.form())


def test__request_body_migration__with_upload_file_read_by_background_task__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    read_by_background_task = []

    async def read_upload(upload: UploadFile):
        read_by_background_task.append(await upload.read())

    @router.post(test_path)
    async def endpoint(
        background_tasks: BackgroundTasks, upload: UploadFile = File(), body: Any = Depends(_parse_form)
    ):
        background_tasks.add_task(read_upload, upload)

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": b"payload"}).status_code == 200
    assert read_by_background_task == [b"payload", b"payload"]


async def _close_upload(upload: UploadFile = File()) -> None:
    await upload.close()


def test__request_body_migration__with_upload_file_closed_by_dependency__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, dependencies=[Depends(_close_upload)])
    async def endpoint(upload: UploadFile = File()):
        return upload.file.closed

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, files={"upload": b"first"}).json() is True


def test__request_by_schema_migration__with_embedded_body__should_raise_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: EmbeddedSchema = Body(embed=True)):
        raise NotImplementedError

    @convert_request_to_next_version_for(EmbeddedSchema)
    def migrator(request: RequestInfo):
        raise NotImplementedError

    # Embedded bodies are wrapped into a separate body schema so only migrations by path apply to them
    with pytest.raises(RouteRequestBySchemaConverterDoesNotApplyToAnythingError):
        create_versioned_clients(version_change(migrator=migrator))


@pytest.mark.parametrize(
    ("alias", "payload"),
    [
        pytest.param(AliasPath("v", "w"), {"v": {"w": 1}}, id="alias_path_key"),
        pytest.param(AliasPath("v", 1), {"v": [0, 1]}, id="alias_path_index"),
        pytest.param(AliasPath("v", -2), {"v": [1, 0]}, id="alias_path_negative_index"),
        pytest.param(AliasChoices(AliasPath("p", 0, "q")), {"p": [{"q": 1}]}, id="alias_choices_with_path_only"),
    ],
)
def test__request_body_migration__with_alias_path_only_validation_aliases__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    alias: Union[AliasPath, AliasChoices],
    payload: dict[str, Any],
):
    class SchemaWithAliasPath(BaseModel):
        value: Optional[int] = Field(default=None, validation_alias=alias)

    @router.post(test_path)
    async def endpoint(body: SchemaWithAliasPath):
        return body.value

    clients = create_versioned_clients(version_change())

    # Known limitation: Cadwyn doesn't build `AliasPath` structures, so the value gets lost like before
    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() is None


@pytest.mark.parametrize("first_annotation", [str, Optional[str]])
def test__request_body_migration__with_alias_paths_and_a_field_missing_in_old_version__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    first_annotation: Any,
):
    class SchemaWithAliasPathsIntoTheSameList(BaseModel):
        first: first_annotation = Field(default="default", validation_alias=AliasPath("names", 0))
        second: Optional[str] = Field(default=None, validation_alias=AliasPath("names", 1))

    @router.post(test_path)
    async def endpoint(body: SchemaWithAliasPathsIntoTheSameList):
        return body.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithAliasPathsIntoTheSameList).field("first").didnt_exist)
    )

    for version_client in clients.values():
        assert version_client.post(test_path, json={"names": ["a", "b"]}).json() == {
            "first": "default",
            "second": None,
        }


def test__request_body_migration__with_alias_path_into_a_field_missing_in_old_version__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class Address(BaseModel):
        city: str
        country: Optional[str] = None

    class SchemaWithAliasPathIntoAnOptionalField(BaseModel):
        address: Optional[Address] = None
        country: Optional[str] = Field(default=None, validation_alias=AliasPath("address", "country"))

    @router.post(test_path)
    async def endpoint(body: SchemaWithAliasPathIntoAnOptionalField):
        return body.model_dump()

    clients = create_versioned_clients(
        version_change(schema(SchemaWithAliasPathIntoAnOptionalField).field("address").didnt_exist)
    )
    payload = {"address": {"country": "US", "city": "X"}}

    assert clients["2000-01-01"].post(test_path, json=payload).json() == {"address": None, "country": None}
    assert clients["2001-01-01"].post(test_path, json=payload).json() == {
        "address": {"city": "X", "country": "US"},
        "country": "US",
    }


def test__request_body_migration__with_alias_paths_into_both_ends_of_a_list__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithAliasPathsIntoBothEnds(BaseModel):
        first: Optional[str] = Field(default=None, validation_alias=AliasPath("names", 0))
        last: Optional[str] = Field(default=None, validation_alias=AliasPath("names", -1))

    @router.post(test_path)
    async def endpoint(body: SchemaWithAliasPathsIntoBothEnds):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        for payload in [{"names": ["a", "b"]}, {"names": ["a"]}]:
            assert version_client.post(test_path, json=payload).json() == {"first": None, "last": None}


def test__request_body_migration__with_extra_allow__should_not_move_client_extras_into_fields(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithExtraAllow(BaseModel):
        model_config = ConfigDict(extra="allow")
        value: Optional[int] = Field(default=None, validation_alias="v", serialization_alias="s")

    @router.post(test_path)
    async def endpoint(body: SchemaWithExtraAllow):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    # Known limitation: the dumped "s" can't be told apart from a client's extra "s", so the value gets lost like before
    for version_client in clients.values():
        for payload in [{"v": 1, "s": 9}, {"s": 9}]:
            assert version_client.post(test_path, json=payload).json() == {"value": None, "s": 9}


class MoneyWithFieldSerializerAndBeforeValidator(BaseModel):
    amount: float = Field(validation_alias="cents", serialization_alias="dollars")

    @field_serializer("amount")
    def to_dollars(self, value: float) -> float:
        return value / 100

    @model_validator(mode="before")
    @classmethod
    def from_dollars(cls, value: Any) -> Any:
        if isinstance(value, dict) and "dollars" in value:
            return {**value, "cents": value["dollars"] * 100}
        return value


class MoneyWithPlainSerializer(BaseModel):
    amount: Annotated[float, PlainSerializer(lambda value: f"${value / 100}")] = Field(
        default=0, validation_alias="cents", serialization_alias="dollars"
    )


class MoneyWithWrapSerializer(BaseModel):
    amount: Annotated[float, WrapSerializer(lambda value, handler: f"${handler(value) / 100}")] = Field(
        default=0, validation_alias="cents", serialization_alias="dollars"
    )


class MoneyWithModelSerializer(BaseModel):
    amount: float = Field(default=0, validation_alias="cents", serialization_alias="dollars")

    @model_serializer
    def to_dollars(self) -> dict[str, Any]:
        return {"dollars": f"${self.amount / 100}"}


class MoneyWithWrapValidator(BaseModel):
    amount: float = Field(default=0, validation_alias="cents", serialization_alias="dollars")

    @model_validator(mode="wrap")
    @classmethod
    def from_dollars(cls, value: Any, handler: Callable[[Any], Any]) -> Any:
        if isinstance(value, dict) and "dollars" in value:
            value = {**value, "cents": value["dollars"] * 1000}
        return handler(value)


class MoneyWithSerializerInNestedModel(BaseModel):
    money: MoneyWithModelSerializer = Field(
        default_factory=MoneyWithModelSerializer, validation_alias="m", serialization_alias="n"
    )


class Cents(BaseModel):
    amount: float = Field(default=0, validation_alias="cents", serialization_alias="dollars")


class MoneyWithPlainSerializerOnNestedModel(BaseModel):
    money: Annotated[Cents, PlainSerializer(lambda value: {"dollars": value.amount / 100})] = Field(
        default_factory=Cents, validation_alias="m", serialization_alias="n"
    )


class MoneyWithWrapSerializerOnNestedModel(BaseModel):
    money: Annotated[Cents, WrapSerializer(lambda value, handler: {"dollars": handler(value)["dollars"] / 100})] = (
        Field(default_factory=Cents, validation_alias="m", serialization_alias="n")
    )


@pytest.mark.parametrize(
    ("model", "payload", "expected"),
    [
        pytest.param(MoneyWithFieldSerializerAndBeforeValidator, {"cents": 100}, 100, id="field_serializer"),
        pytest.param(MoneyWithPlainSerializer, {"cents": 100}, 0, id="plain_serializer"),
        pytest.param(MoneyWithWrapSerializer, {"cents": 100}, 0, id="wrap_serializer"),
        pytest.param(MoneyWithModelSerializer, {"cents": 100}, 0, id="model_serializer"),
        pytest.param(MoneyWithWrapValidator, {"cents": 100}, 100_000, id="wrap_model_validator"),
    ],
)
def test__request_body_migration__with_custom_serialization_or_model_validators__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    payload: dict[str, Any],
    expected: float,
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return body.amount

    clients = create_versioned_clients(version_change())

    # Known limitation: a serialized value might not be valid validation input, so we leave it as before
    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == expected


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(MoneyWithSerializerInNestedModel, id="model_serializer"),
        # The serializer is attached to the reference to the nested model, not to the nested model itself
        pytest.param(MoneyWithPlainSerializerOnNestedModel, id="plain_serializer_on_nested_model"),
        pytest.param(MoneyWithWrapSerializerOnNestedModel, id="wrap_serializer_on_nested_model"),
    ],
)
def test__request_body_migration__with_custom_serialization_in_nested_model__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return body.money.amount

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"m": {"cents": 100}}).json() == 0


class SchemaWithItemsThatAreOmittedOnError(BaseModel):
    items: list[OnErrorOmit[InnerSchemaWithValidationAlias]] = []
    # Required, so that the head surely rejects the body as it is and we restore it
    name: str = Field(validation_alias="n")


def test__request_body_migration__with_items_that_are_omitted_on_error__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithItemsThatAreOmittedOnError):
        return [body.name, len(body.items)]

    clients = create_versioned_clients(version_change())

    # `OnErrorOmit` omits the items that fail validation instead of failing, so we can't tell whether
    # the dumped keys made them fail. We leave them as before.
    for version_client in clients.values():
        assert version_client.post(test_path, json={"items": [{"v": 1}], "n": "name"}).json() == ["name", 0]


class CentsWithPlainSerializer(BaseModel):
    amount: Annotated[float, PlainSerializer(lambda value: value / 100)] = 0


class MoneyWithSerializerTwoLevelsDeep(BaseModel):
    with_serializer: CentsWithPlainSerializer = Field(default_factory=CentsWithPlainSerializer)
    without_serializer: Cents = Field(default_factory=Cents)


class MoneyWithSerializerTwoLevelsDeepAndSafeMoney(BaseModel):
    money: MoneyWithSerializerTwoLevelsDeep = Field(
        default_factory=MoneyWithSerializerTwoLevelsDeep, validation_alias="m", serialization_alias="n"
    )
    # Required, so that the head surely rejects the body as it is and we restore it
    other_money: Cents = Field(validation_alias="o", serialization_alias="p")


def test__request_body_migration__with_custom_serialization_two_levels_deep__should_only_restore_safe_fields(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: MoneyWithSerializerTwoLevelsDeepAndSafeMoney):
        return [body.money.with_serializer.amount, body.money.without_serializer.amount, body.other_money.amount]

    clients = create_versioned_clients(version_change())

    payload = {"m": {"with_serializer": {"amount": 100}, "without_serializer": {"cents": 100}}, "o": {"cents": 100}}
    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == [0, 0, 100]


@pytest.mark.parametrize(
    ("config", "serialization_alias"),
    [
        pytest.param(ConfigDict(), "s", id="serialization_alias"),
        pytest.param(
            ConfigDict(alias_generator=AliasGenerator(serialization_alias=lambda _: "s")), None, id="generator"
        ),
    ],
)
def test__request_body_migration__with_fields_sharing_a_dumped_key__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    config: ConfigDict,
    serialization_alias: Optional[str],
):
    class SchemaWithSharedDumpedKey(BaseModel):
        model_config = config
        a: int = Field(default=0, validation_alias="v", serialization_alias=serialization_alias)
        b: int = Field(default=0, validation_alias="w", serialization_alias=serialization_alias)

    @router.post(test_path)
    async def endpoint(body: SchemaWithSharedDumpedKey):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    # Known limitation: we can't know which field a shared dumped key belongs to, so we leave it as before
    for version_client in clients.values():
        assert version_client.post(test_path, json={"v": 1, "w": 2}).json() == {"a": 0, "b": 0}


def test__request_body_migration__with_chained_validation_aliases__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithChainedValidationAliases(BaseModel):
        a: Optional[int] = Field(default=None, validation_alias="b")
        b: Optional[int] = Field(default=None, validation_alias="c")

    @router.post(test_path)
    async def endpoint(body: SchemaWithChainedValidationAliases):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    # Known limitation: a dumped key that another field validates is ambiguous, so we leave it as before
    for version_client in clients.values():
        assert version_client.post(test_path, json={"b": 1, "c": 2}).json() == {"a": 2, "b": None}


def test__request_body_migration__that_sets_an_alias_path_to_none__should_keep_it(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithNullableAliasPath(BaseModel):
        value: Optional[int] = Field(default=None, validation_alias=AliasPath("nested", "v"))

    @router.post(test_path)
    async def endpoint(body: SchemaWithNullableAliasPath):
        return body.value

    @convert_request_to_next_version_for(SchemaWithNullableAliasPath)
    def migrator(request: RequestInfo):
        request.body["nested"] = {"v": None}

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json={"nested": {"v": 7}}).json() is None


@pytest.mark.parametrize(
    ("alias", "payload"),
    [
        pytest.param(AliasChoices(AliasPath("nested", "v"), AliasPath("other", "v")), {"nested": {"v": 7}}, id="paths"),
        pytest.param(AliasChoices("w", AliasPath("other", "v")), {"w": 7}, id="key_and_path"),
    ],
)
def test__request_body_migration__that_sets_a_later_alias_choice__head_should_get_its_value_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    alias: AliasChoices,
    payload: dict[str, Any],
):
    class SchemaWithAliasChoices(BaseModel):
        value: int = Field(default=0, validation_alias=alias)

    @router.post(test_path)
    async def endpoint(body: SchemaWithAliasChoices):
        return body.value

    @convert_request_to_next_version_for(SchemaWithAliasChoices)
    def migrator(request: RequestInfo):
        request.body["other"] = {"v": 99}

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json=payload).json() == 99


class CentsWithDollarsSerialization(BaseModel):
    cents: int = Field(default=0, serialization_alias="dollars")


def _decode_dollars(value: Any) -> Any:
    if isinstance(value, dict) and "dollars" in value:
        return {"cents": int(value["dollars"] * 100)}
    return value


def _encode_dollars(value: CentsWithDollarsSerialization) -> dict[str, float]:
    return {"dollars": value.cents / 100}


class MoneyWithAnnotatedHooks(BaseModel):
    money: Annotated[CentsWithDollarsSerialization, BeforeValidator(_decode_dollars), PlainSerializer(_encode_dollars)]


class MoneyParentWithHooks(BaseModel):
    money: CentsWithDollarsSerialization

    @field_serializer("money")
    def serialize_money(self, value: CentsWithDollarsSerialization) -> dict[str, float]:
        return _encode_dollars(value)

    @model_validator(mode="before")
    @classmethod
    def deserialize_money(cls, value: dict[str, Any]) -> dict[str, Any]:
        return {**value, "money": _decode_dollars(value["money"])}


class MoneyWithInheritedHooks(MoneyParentWithHooks):
    pass


class IntWithCustomCoreSchema(int):
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.int_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(lambda value: f"{value} cents"),
        )


IntWithSerializerAlias = TypeAliasType(
    "IntWithSerializerAlias", Annotated[int, PlainSerializer(lambda value: f"#{value}", return_type=str)]
)


@pydantic_dataclass
class DataclassWithFieldSerializer:
    x: int

    @field_serializer("x")
    def serialize_x(self, value: int) -> str:
        return f"x={value}"


class InnerWithComputedFieldAndExtraForbid(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: int

    @computed_field
    @property
    def double(self) -> int:
        return self.a * 2


class InnerWithModelSerializerInParent(BaseModel):
    @model_serializer(mode="wrap")
    def serialize_with_type(self, handler: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        return {**handler(self), "_t": type(self).__name__}


class InnerWithModelSerializerInParentChild(InnerWithModelSerializerInParent):
    q: Optional[int] = Field(default=None, validation_alias="qq")


class InnerWithAlias(BaseModel):
    a: Optional[int] = Field(default=None, alias="x", validation_alias="v")


class ItemWithWireSerialization(BaseModel):
    value: int = Field(serialization_alias="wire")


def _decode_wire(data: dict[str, Any]) -> dict[str, Any]:
    # Fails if it gets anything but the serialized value
    return {"value": data["wire"]}


class SchemaWithBeforeValidatorInList(BaseModel):
    items: list[Annotated[ItemWithWireSerialization, BeforeValidator(_decode_wire)]]


class SchemaWithBeforeValidatorInOptional(BaseModel):
    item: Optional[Annotated[ItemWithWireSerialization, BeforeValidator(_decode_wire)]] = None


class SchemaWithChainCoreSchema(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.chain_schema([handler(source)])


class SchemaWithJsonOrPythonCoreSchema(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        schema = handler(source)
        return core_schema.json_or_python_schema(json_schema=schema, python_schema=schema)


class SchemaWithLaxOrStrictCoreSchema(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        schema = handler(source)
        return core_schema.lax_or_strict_schema(lax_schema=schema, strict_schema=schema)


class SchemaWithNullableCoreSchema(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.nullable_schema(handler(source))


class SchemaWithUnionCoreSchema(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.union_schema([handler(source), core_schema.none_schema()])


class SchemaWithJson(BaseModel):
    value: Json[list[int]] = Field(default_factory=list, validation_alias="v", serialization_alias="s")


class SchemaWithCustomCoreSchemaType(BaseModel):
    value: IntWithCustomCoreSchema = Field(default=IntWithCustomCoreSchema(0), validation_alias="v")


class SchemaWithSerializerInTypeAlias(BaseModel):
    value: Optional[IntWithSerializerAlias] = Field(default=None, validation_alias="v")


class SchemaWithDataclassWithFieldSerializer(BaseModel):
    value: Optional[DataclassWithFieldSerializer] = Field(default=None, validation_alias="v")


class SchemaWithComputedFieldInExtraForbidModel(BaseModel):
    value: Optional[InnerWithComputedFieldAndExtraForbid] = Field(default=None, validation_alias="v")


class SchemaWithModelSerializerInNestedParentClass(BaseModel):
    inner: Optional[InnerWithModelSerializerInParentChild] = None


class SchemaWithInheritedFieldSerializer(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @field_serializer("value")
    def serialize_value(self, value: Optional[int]) -> str:
        return f"#{value}"


class SchemaWithInheritedFieldSerializerChild(SchemaWithInheritedFieldSerializer):
    pass


class SchemaWithBeforeFieldValidator(BaseModel):
    tags: list[str] = Field(default_factory=list, validation_alias="t")

    @field_validator("tags", mode="before")
    @classmethod
    def split(cls, value: Any) -> Any:
        return value.split(",")


class SchemaWithBeforeValidatorInAnnotated(BaseModel):
    tags: Annotated[list[str], BeforeValidator(lambda value: value.split(","))] = Field(
        default_factory=list, validation_alias="t"
    )


class SchemaWithWrapFieldValidator(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @field_validator("value", mode="wrap")
    @classmethod
    def count(cls, value: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(len(value)) if isinstance(value, str) else handler(value) * 1000


class SchemaWithV1PreRootValidator(BaseModel):
    value: Optional[int] = Field(default=None, alias="x", validation_alias="v")

    @root_validator(pre=True)  # ty: ignore[deprecated]  # V1 validators are still supported.
    def from_dumped_key(cls, values: dict[str, Any]) -> dict[str, Any]:
        if "x" in values:
            values = {**values, "v": values["x"] * 10}
        return values


class SchemaWithV1PreValidator(BaseModel):
    tags: list[str] = Field(default_factory=list, validation_alias="t")

    @validator("tags", pre=True)  # ty: ignore[deprecated]  # V1 validators are still supported.
    def split(cls, value: Any) -> Any:
        return value.split(",")


class SchemaWithBeforeValidatorReadingNestedDumpedKey(BaseModel):
    inner: Optional[InnerWithAlias] = None

    @model_validator(mode="before")
    @classmethod
    def from_dumped_key(cls, data: Any) -> Any:
        inner = data.get("inner") if isinstance(data, dict) else None
        if isinstance(inner, dict) and "x" in inner:
            return {**data, "inner": {"v": inner["x"] * 10}}
        return data


class SchemaWithFieldValidatorReadingNestedDumpedKey(BaseModel):
    inner: Optional[InnerWithAlias] = None

    @field_validator("inner", mode="before")
    @classmethod
    def from_dumped_key(cls, value: Any) -> Any:
        if isinstance(value, dict) and "v" not in value:
            return {"v": value["x"] * 10}
        return value


class RootWithBeforeValidator(RootModel[Annotated[list[InnerWithAlias], BeforeValidator(lambda value: value)]]):
    pass


class InnerWithSerializationAlias(BaseModel):
    x: int = Field(default=0, serialization_alias="y")


class SchemaWithAliasPathIntoNestedModel(BaseModel):
    inner: InnerWithSerializationAlias
    copied: int = Field(default=0, validation_alias=AliasPath("inner", "y"))


class OtherInnerWithY(BaseModel):
    y: int


class SchemaWithNestedModelsSharingAnAlias(BaseModel):
    a: InnerWithSerializationAlias = Field(alias="shared")
    b: OtherInnerWithY = Field(alias="shared")


@pytest.mark.parametrize(
    ("model", "payload", "read", "expected"),
    [
        pytest.param(
            MoneyWithAnnotatedHooks, {"money": {"cents": 100}}, lambda b: b.money.cents, 100, id="annotated_hooks"
        ),
        pytest.param(
            MoneyWithInheritedHooks, {"money": {"cents": 100}}, lambda b: b.money.cents, 100, id="inherited_hooks"
        ),
        pytest.param(SchemaWithJson, {"v": "[1,2]"}, lambda b: b.value, [], id="json"),
        pytest.param(
            SchemaWithCustomCoreSchemaType, {"v": 100}, lambda b: int(b.value), 0, id="custom_core_schema_type"
        ),
        pytest.param(SchemaWithSerializerInTypeAlias, {"v": 5}, lambda b: b.value, None, id="serializer_in_type_alias"),
        pytest.param(SchemaWithDataclassWithFieldSerializer, {"v": {"x": 1}}, lambda b: b.value, None, id="dataclass"),
        pytest.param(
            SchemaWithComputedFieldInExtraForbidModel, {"v": {"a": 1}}, lambda b: b.value, None, id="computed"
        ),
        pytest.param(
            SchemaWithModelSerializerInNestedParentClass,
            {"inner": {"qq": 1}},
            lambda b: b.inner.q,
            None,
            id="model_serializer_in_nested_parent_class",
        ),
        pytest.param(
            SchemaWithInheritedFieldSerializerChild, {"v": 5}, lambda b: b.value, None, id="inherited_serializer"
        ),
        pytest.param(SchemaWithBeforeFieldValidator, {"t": "a,b"}, lambda b: b.tags, [], id="before_field_validator"),
        pytest.param(
            SchemaWithBeforeValidatorInAnnotated, {"t": "a,b"}, lambda b: b.tags, [], id="annotated_validator"
        ),
        pytest.param(SchemaWithWrapFieldValidator, {"v": "abc"}, lambda b: b.value, None, id="wrap_field_validator"),
        pytest.param(SchemaWithV1PreRootValidator, {"v": 1}, lambda b: b.value, 10, id="v1_pre_root_validator"),
        pytest.param(SchemaWithV1PreValidator, {"t": "a,b"}, lambda b: b.tags, [], id="v1_pre_validator"),
        pytest.param(
            SchemaWithBeforeValidatorReadingNestedDumpedKey,
            {"inner": {"v": 1}},
            lambda b: b.inner.a,
            10,
            id="model_validator_reading_nested_dumped_key",
        ),
        pytest.param(
            SchemaWithFieldValidatorReadingNestedDumpedKey,
            {"inner": {"v": 1}},
            lambda b: b.inner.a,
            10,
            id="field_validator_reading_nested_dumped_key",
        ),
        pytest.param(
            SchemaWithBeforeValidatorInList,
            {"items": [{"wire": 7}]},
            lambda b: [item.value for item in b.items],
            [7],
            id="before_validator_in_list",
        ),
        pytest.param(
            SchemaWithBeforeValidatorInOptional,
            {"item": {"wire": 7}},
            lambda b: b.item.value,
            7,
            id="before_validator_in_optional",
        ),
        pytest.param(SchemaWithChainCoreSchema, {"v": 1}, lambda b: b.value, None, id="chain_core_schema"),
        pytest.param(SchemaWithJsonOrPythonCoreSchema, {"v": 1}, lambda b: b.value, None, id="json_or_python"),
        pytest.param(SchemaWithLaxOrStrictCoreSchema, {"v": 1}, lambda b: b.value, None, id="lax_or_strict"),
        pytest.param(SchemaWithNullableCoreSchema, {"v": 1}, lambda b: b.value, None, id="nullable_core_schema"),
        pytest.param(SchemaWithUnionCoreSchema, {"v": 1}, lambda b: b.value, None, id="union_core_schema"),
        pytest.param(
            RootWithBeforeValidator, [{"v": 1}], lambda b: [i.a for i in b.root], [None], id="root_before_validator"
        ),
        pytest.param(
            SchemaWithAliasPathIntoNestedModel,
            {"inner": {"x": 7, "y": 7}},
            lambda b: [b.inner.x, b.copied],
            [0, 7],
            id="alias_path_into_nested_model",
        ),
        pytest.param(
            SchemaWithNestedModelsSharingAnAlias,
            {"shared": {"x": 1, "y": 2}},
            lambda b: [b.a.x, b.b.y],
            [0, 2],
            id="nested_models_sharing_an_alias",
        ),
    ],
)
def test__request_body_migration__with_values_that_might_not_survive_the_dump__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    payload: Any,
    read: Callable[[Any], Any],
    expected: Any,
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return read(body)

    clients = create_versioned_clients(version_change())

    # Known limitation: Cadwyn can't know whether such dumped values are valid input, so it leaves them as before
    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == expected


def test__request_body_migration__with_union_of_models_as_body__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: Union[OtherInnerSchema, InnerSchemaWithValidationAlias]):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"o": 1}).json() == {"other_value": 1}


def test__request_body_migration__with_after_model_validator_and_extra_forbid__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class InnerWithExtraForbid(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int = Field(alias="x", validation_alias="v")

    class SchemaWithAfterValidator(BaseModel):
        inner: InnerWithExtraForbid

        @model_validator(mode="after")
        def check(self) -> Self:
            assert self.inner.value > 0
            return self

    @router.post(test_path)
    async def endpoint(body: SchemaWithAfterValidator):
        return body.inner.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"inner": {"v": 1}}).json() == 1


@pytest.mark.parametrize(
    "default",
    [
        pytest.param({"type": "definitions"}, id="definitions"),
        pytest.param({"type": "definition-ref", "schema_ref": "missing"}, id="definition_ref"),
        pytest.param({"type": "function-before", "schema": {"type": "int"}}, id="function_before"),
    ],
)
def test__request_body_migration__with_defaults_shaped_like_core_schemas__should_not_read_them(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    default: dict[str, Any],
):
    class SchemaWithCoreSchemaShapedDefault(BaseModel):
        options: dict[str, Any] = default
        # Required, so that the head surely rejects the body as it is and we restore it
        value: int = Field(validation_alias="v")

    @router.post(test_path)
    async def endpoint(body: SchemaWithCoreSchemaShapedDefault):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"v": 1}).json() == {"options": default, "value": 1}
        assert version_client.post(test_path, json={"options": {}, "v": 1}).json() == {"options": {}, "value": 1}


class TypedDictWithValue(TypedDict):
    value: int


@pydantic_dataclass
class DataclassWithValue:
    value: int


class Cat(BaseModel):
    kind: Literal["cat"] = "cat"


class Dog(BaseModel):
    kind: Literal["dog"] = "dog"


class StrEnumWithValue(str, Enum):
    value = "value"


def test__request_body_migration__with_fields_of_all_kinds_of_core_schemas__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithAllKindsOfFields(BaseModel):
        tuple_: tuple[int, ...] = Field(validation_alias="tuple_v")
        fixed_tuple: tuple[int, str] = Field(validation_alias="fixed_tuple_v")
        set_: set[int] = Field(validation_alias="set_v")
        frozenset_: frozenset[int] = Field(validation_alias="frozenset_v")
        dict_: dict[str, Optional[int]] = Field(validation_alias="dict_v")
        union: Union[int, str] = Field(validation_alias="union_v")
        tagged_union: Annotated[Union[Cat, Dog], Field(discriminator="kind")] = Field(validation_alias="tagged_union_v")
        typed_dict: TypedDictWithValue = Field(validation_alias="typed_dict_v")
        dataclass: DataclassWithValue = Field(validation_alias="dataclass_v")
        strict: Annotated[int, Strict()] = Field(validation_alias="strict_v")
        literal: Literal["a", "b"] = Field(validation_alias="literal_v")
        enum: StrEnumWithValue = Field(validation_alias="enum_v")
        decimal: Decimal = Field(validation_alias="decimal_v")
        uuid: UUID = Field(validation_alias="uuid_v")
        datetime_: datetime = Field(validation_alias="datetime_v")
        any_: Any = Field(validation_alias="any_v")

    @router.post(test_path)
    async def endpoint(body: SchemaWithAllKindsOfFields):
        return body.model_dump(mode="json")

    clients = create_versioned_clients(version_change())
    values = {
        "tuple_": [1, 2],
        "fixed_tuple": [1, "a"],
        "set_": [1],
        "frozenset_": [2],
        "dict_": {"a": 1, "b": None},
        "union": "a",
        "tagged_union": {"kind": "dog"},
        "typed_dict": {"value": 1},
        "dataclass": {"value": 2},
        "strict": 3,
        "literal": "b",
        "enum": "value",
        "decimal": "1.5",
        "uuid": "12345678-1234-5678-1234-567812345678",
        "datetime_": "2000-01-01T00:00:00",
        "any_": {"type": "definitions"},
    }

    for version_client in clients.values():
        payload = {f"{name.rstrip('_')}_v": value for name, value in values.items()}
        assert version_client.post(test_path, json=payload).json() == values


@pytest.mark.parametrize(
    "core_schema_",
    [
        pytest.param(None, id="none"),
        pytest.param({}, id="empty"),
        pytest.param({"type": "model"}, id="model_without_keys"),
        pytest.param({"type": "definitions"}, id="definitions_without_keys"),
        pytest.param({"type": "definitions", "definitions": [], "schema": {"type": "definition-ref"}}, id="bad_ref"),
        pytest.param({"type": "function-after", "schema": None}, id="bad_wrapper"),
        pytest.param({"type": "model", "cls": None, "root_model": False, "schema": {}}, id="other_class"),
    ],
)
def test__request_body_migration__with_model_core_schema_of_unexpected_shape__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    core_schema_: Any,
):
    class SchemaWithUnexpectedCoreSchema(BaseModel):
        value: Optional[int] = Field(default=None, validation_alias="v")

    @router.post(test_path)
    async def endpoint(body: SchemaWithUnexpectedCoreSchema):
        return body.value

    clients = create_versioned_clients(version_change())
    # Pydantic doesn't produce such schemas, but Cadwyn must not fail on them either
    SchemaWithUnexpectedCoreSchema.__pydantic_core_schema__ = core_schema_

    for version_client in clients.values():
        assert version_client.post(test_path, json={"v": 1}).json() is None


@pytest.mark.parametrize(
    "field_core_schema",
    [
        pytest.param(None, id="none"),
        pytest.param({"type": "list", "items_schema": "garbage"}, id="garbage_child"),
        pytest.param({"type": "tuple", "items_schema": 1}, id="garbage_children"),
        pytest.param({"type": "definition-ref", "schema_ref": "missing"}, id="missing_ref"),
        pytest.param({"type": "model", "schema": None}, id="model_without_fields"),
        pytest.param({"type": "model", "cls": object(), "schema": "garbage"}, id="garbage_nested_model"),
        pytest.param({"type": "unknown"}, id="unknown_type"),
    ],
)
def test__request_body_migration__with_field_core_schema_of_unexpected_shape__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    field_core_schema: Any,
):
    class SchemaWithUnexpectedFieldCoreSchema(BaseModel):
        value: Optional[int] = Field(default=None, validation_alias="v")
        # Required, so that the head surely rejects the body as it is and we restore it
        other: int = Field(validation_alias="o")

    @router.post(test_path)
    async def endpoint(body: SchemaWithUnexpectedFieldCoreSchema):
        return body.model_dump()

    clients = create_versioned_clients(version_change())
    # Pydantic doesn't produce such schemas, but Cadwyn must not fail on them either
    original_schema: Any = SchemaWithUnexpectedFieldCoreSchema.__pydantic_core_schema__
    fields = {
        **original_schema["schema"]["fields"],
        "value": {"type": "model-field", "schema": field_core_schema, "validation_alias": "v"},
    }
    unexpected_schema: Any = {**original_schema, "schema": {**original_schema["schema"], "fields": fields}}
    SchemaWithUnexpectedFieldCoreSchema.__pydantic_core_schema__ = unexpected_schema

    for version_client in clients.values():
        assert version_client.post(test_path, json={"v": 1, "o": 2}).json() == {"value": None, "other": 2}


def test__request_body_migration__with_nested_model_of_unexpected_core_schema__should_restore_its_siblings(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithNestedChainCoreSchema(BaseModel):
        inner: SchemaWithChainCoreSchema
        # Required, so that the head surely rejects the body as it is and we restore it
        other: int = Field(validation_alias="o")

    @router.post(test_path)
    async def endpoint(body: SchemaWithNestedChainCoreSchema):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"inner": {"v": 1}, "o": 2}).json() == {
            "inner": {"value": None},
            "other": 2,
        }


class InnerWithV1PostRootValidator(BaseModel):
    q: Optional[int] = Field(default=None, validation_alias="qq")

    @root_validator(skip_on_failure=True)  # ty: ignore[deprecated]  # V1 validators are still supported.
    def check(cls, values: dict[str, Any]) -> dict[str, Any]:
        return values


class SchemaWithV1PostRootValidator(BaseModel):
    inner: Optional[InnerWithV1PostRootValidator] = None
    # Required, so that the head surely rejects the body as it is and we restore it
    value: int = Field(validation_alias="v")

    @root_validator(skip_on_failure=True)  # ty: ignore[deprecated]  # V1 validators are still supported.
    def check(cls, values: dict[str, Any]) -> dict[str, Any]:
        return values


def test__request_body_migration__with_v1_post_root_validators__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithV1PostRootValidator):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"inner": {"qq": 1}, "v": 2}).json() == {
            "inner": {"q": 1},
            "value": 2,
        }


def test__request_body_migration__with_renamed_field_of_model_without_aliases__head_should_get_it(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithRenamedModelField(BaseModel):
        inner: OtherInnerWithY = Field(alias="i", validation_alias="j")

    @router.post(test_path)
    async def endpoint(body: SchemaWithRenamedModelField):
        return body.inner.y

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"j": {"y": 1}}).json() == 1


def test__request_body_migration__with_recursive_model__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    # Python 3.10 keeps forward references inside builtin generics such as `list["Node"]` unevaluated
    # in the field info, so only references that typing evaluates, such as `Optional["Node"]`, are walked into there
    class Node(BaseModel):
        # Required, so that the head surely rejects the body as it is and we restore it
        value: int = Field(validation_alias="vv")
        child: Optional["Node"] = Field(default=None, validation_alias="kid")

    @router.post(test_path)
    async def endpoint(body: Node):
        return body.model_dump()

    clients = create_versioned_clients(version_change())
    payload = {"vv": 1, "kid": {"vv": 2, "kid": {"vv": 3}}}

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == {
            "value": 1,
            "child": {"value": 2, "child": {"value": 3, "child": None}},
        }


def test__request_body_migration__with_custom_serialization_shared_by_nested_models__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class FirstWrapper(BaseModel):
        money: MoneyWithModelSerializer = Field(default_factory=MoneyWithModelSerializer)

    class SecondWrapper(BaseModel):
        money: MoneyWithModelSerializer = Field(default_factory=MoneyWithModelSerializer)

    class SchemaWithWrappers(BaseModel):
        first: FirstWrapper = Field(default_factory=FirstWrapper, validation_alias="f")
        second: SecondWrapper = Field(default_factory=SecondWrapper, validation_alias="s")

    @router.post(test_path)
    async def endpoint(body: SchemaWithWrappers):
        return [body.first.money.amount, body.second.money.amount]

    clients = create_versioned_clients(version_change())
    payload = {"f": {"money": {"cents": 100}}, "s": {"money": {"cents": 200}}}

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == [0, 0]


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(Annotated[ItemWithWireSerialization, BeforeValidator(_decode_wire)], id="before"),
        pytest.param(
            Annotated[ItemWithWireSerialization, WrapValidator(lambda value, handler: handler(_decode_wire(value)))],
            id="wrap",
        ),
        pytest.param(
            Annotated[
                ItemWithWireSerialization,
                PlainValidator(lambda value: ItemWithWireSerialization.model_validate(_decode_wire(value))),
            ],
            id="plain",
        ),
        pytest.param(Optional[Annotated[ItemWithWireSerialization, BeforeValidator(_decode_wire)]], id="optional"),
    ],
)
def test__request_body_migration__with_validators_on_the_body_parameter__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
):
    @router.post(test_path)
    async def endpoint(body: annotation):
        return body.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"wire": 7}).json() == 7


class ItemWithCustomInit(BaseModel):
    value: int = Field(serialization_alias="wire")

    def __init__(self, **data: Any) -> None:
        super().__init__(value=data["wire"])


class SchemaWithNestedCustomInit(BaseModel):
    item: ItemWithCustomInit


@pytest.mark.parametrize(
    ("model", "payload", "read"),
    [
        pytest.param(ItemWithCustomInit, {"value": 7}, lambda body: body.value, id="body"),
        pytest.param(SchemaWithNestedCustomInit, {"item": {"value": 7}}, lambda body: body.item.value, id="nested"),
    ],
)
def test__request_body_migration__with_custom_init__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    payload: dict[str, Any],
    read: Callable[[Any], Any],
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return read(body)

    clients = create_versioned_clients(version_change())

    # The versioned copies validate the field name, while the head's `__init__` expects the dumped key
    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == 7


def test__request_body_migration__with_bulk_items_without_the_renamed_keys__head_should_get_them_unchanged(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class Item(BaseModel):
        id: int
        optional: int = Field(default=0, validation_alias="wire")

    class SchemaWithItems(BaseModel):
        items: list[Item]
        by_name: dict[str, Item] = Field(default_factory=dict)
        # Required, so that the head surely rejects the body as it is and we restore all of it
        name: str = Field(validation_alias="n")

    @router.post(test_path)
    async def endpoint(body: SchemaWithItems):
        return body.model_dump()

    clients = create_versioned_clients(version_change())
    payload = {"items": [{"id": 1}, {"id": 2, "wire": 3}, {"id": 4}], "by_name": {"a": {"id": 5}}, "n": "name"}

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == {
            "name": "name",
            "items": [{"id": 1, "optional": 0}, {"id": 2, "optional": 3}, {"id": 4, "optional": 0}],
            "by_name": {"a": {"id": 5, "optional": 0}},
        }


class SchemaWithComputedFieldCollidingWithDumpedKey(BaseModel):
    value: int = Field(default=0, validation_alias="v")

    @computed_field(alias="value")
    @property
    def display(self) -> str:
        return f"{self.value} cents"


class SchemaWithComputedFieldCollidingWithTargetKey(BaseModel):
    value: int = Field(default=0, validation_alias="v")

    @computed_field(alias="v")
    @property
    def display(self) -> int:
        return 123


class SchemaWithModelDumpOverride(BaseModel):
    value: Optional[int] = Field(default=None, validation_alias="v")

    @override
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {**BaseModel.model_dump(self, **kwargs), "value": "redacted"}


class InnerWithModelDumpOverride(SchemaWithModelDumpOverride):
    pass


class SchemaWithNestedModelDumpOverride(BaseModel):
    inner: Optional[InnerWithModelDumpOverride] = Field(default=None, validation_alias="i")


class InnerWithComputedFieldAndV1PostRootValidator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int

    @root_validator(skip_on_failure=True)  # ty: ignore[deprecated]  # V1 validators are still supported.
    def check(cls, values: dict[str, Any]) -> dict[str, Any]:
        return values

    @computed_field
    @property
    def display(self) -> str:
        return str(self.value)


class SchemaWithComputedFieldBehindV1PostRootValidator(BaseModel):
    inner: Optional[InnerWithComputedFieldAndV1PostRootValidator] = Field(default=None, validation_alias="v")


@pytest.mark.parametrize(
    ("model", "payload", "read", "expected"),
    [
        pytest.param(
            SchemaWithComputedFieldCollidingWithDumpedKey,
            {"v": 0},
            lambda b: b.value,
            0,
            id="computed_field_on_dumped_key",
        ),
        pytest.param(
            SchemaWithComputedFieldCollidingWithTargetKey,
            {"v": 5},
            lambda b: b.value,
            123,
            id="computed_field_on_target_key",
        ),
        pytest.param(SchemaWithModelDumpOverride, {"v": None}, lambda b: b.value, None, id="model_dump_override"),
        pytest.param(
            SchemaWithNestedModelDumpOverride,
            {"i": {"v": None}},
            lambda b: b.inner,
            None,
            id="nested_model_dump_override",
        ),
        pytest.param(
            SchemaWithComputedFieldBehindV1PostRootValidator,
            {"v": {"value": 7}},
            lambda b: b.inner,
            None,
            id="computed_field_behind_v1_post_root_validator",
        ),
    ],
)
def test__request_body_migration__with_dumps_that_the_core_schema_does_not_describe__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    payload: dict[str, Any],
    read: Callable[[Any], Any],
    expected: Any,
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return read(body)

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == expected


class DeepNodeWithValidationAlias(BaseModel):
    value: Optional[int] = Field(default=None, alias="x", validation_alias="vv")
    kids: dict[str, list["DeepNodeWithValidationAlias"]] = {}


def _make_deep_node_payload(depth: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"vv": 1}
    for _ in range(depth):
        payload = {"vv": 1, "kids": {"k": [payload]}}
    return payload


def _get_max_depth_that_pydantic_accepts() -> int:
    """Pydantic's recursion guard depends on the platform and the thread's stack, so we find its limit by bisection"""
    accepted, rejected = 0, 1000
    while rejected - accepted > 1:
        depth = (accepted + rejected) // 2
        try:
            DeepNodeWithValidationAlias.model_validate(_make_deep_node_payload(depth))
        except ValidationError:
            rejected = depth
        else:
            accepted = depth
    return accepted


def test__request_body_migration__with_body_as_deep_as_pydantic_accepts__head_should_get_it(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: DeepNodeWithValidationAlias):
        depth = 0
        while body.kids:
            body = body.kids["k"][0]
            depth += 1
        return depth

    # Requests run in another thread, which can have a smaller stack, so we measure pydantic's limit there
    @router.get("/max_depth")
    async def get_max_depth() -> int:
        return _get_max_depth_that_pydantic_accepts()

    clients = create_versioned_clients(version_change())
    # Restoring it recursively would take several Python frames per level, which exceeded the recursion limit
    # at about 245 levels where pydantic accepts 254. The margin covers the extra stack of the body's validation.
    depth = clients["2000-01-01"].get("/max_depth").json() - 5
    payload = _make_deep_node_payload(depth)

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == depth


class TreeNodeWithValidationAlias(BaseModel):
    value: Optional[int] = Field(default=None, alias="x", validation_alias="vv")
    # Python 3.10 keeps quoted names inside `list[...]` as strings, so we quote the whole annotation
    kids: "dict[str, list[TreeNodeWithValidationAlias]]" = {}


def _make_nested_model_chain(name: str, depth: int) -> tuple[type[BaseModel], dict[str, Any]]:
    # Required, so that the head surely rejects the body as it is and we restore it
    model: type[BaseModel] = create_model(f"{name}Leaf", value=(int, Field(alias="x", validation_alias="v")))
    payload: dict[str, Any] = {"v": 7}
    for i in range(depth):
        model = create_model(f"{name}{i}", child=(model, ...), value=(int, Field(alias="x", validation_alias="v")))
        payload = {"child": payload, "v": 7}
    return model, payload


def test__request_body_migration__with_nested_models_seen_for_the_first_time__should_not_need_a_deeper_stack(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
):
    """Reading the models of a body for the first time must not take a deeper stack than restoring it afterwards.

    Otherwise, requests with deeply nested bodies could hit the recursion limit only when they come first.
    """
    seen_model, seen_payload = _make_nested_model_chain("Seen", depth=50)
    new_model, new_payload = _make_nested_model_chain("New", depth=50)

    @router.post("/seen")
    async def seen(body: seen_model):  # ty: ignore[invalid-type-form]
        return body.value

    @router.post("/new")
    async def new(body: new_model):  # ty: ignore[invalid-type-form]
        return body.value

    app = create_versioned_app(version_change())
    max_depths: list[int] = []

    async def app_with_measured_stack(scope: Any, receive: Any, send: Any) -> None:
        """Measure how deep the stack gets above the app while it handles the request, in the thread that runs it"""
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        depth = max_depth = 0

        # Coverage doesn't trace profile hooks
        def profile(frame: Any, event: str, arg: Any) -> None:  # pragma: no cover
            nonlocal depth, max_depth
            if event == "call":
                depth += 1
                max_depth = max(max_depth, depth)
            elif event == "return":
                depth -= 1

        sys.setprofile(profile)
        try:
            await app(scope, receive, send)
        finally:
            sys.setprofile(None)
        max_depths.append(max_depth)

    headers = {app.router.api_version_parameter_name: "2000-01-01"}
    with TestClient(app_with_measured_stack, headers=headers) as client:
        assert client.post("/seen", json=seen_payload).json() == 7
        assert client.post("/seen", json=seen_payload).json() == 7
        assert client.post("/new", json=new_payload).json() == 7
    depth_with_known_models, depth_with_new_models = max_depths[1:]

    # Reading the models takes a few frames, but not a frame per nested model
    assert depth_with_new_models <= depth_with_known_models + 20


def test__response_migration__with_self_referencing_response_model_that_passes_validation__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=TreeNodeWithValidationAlias, response_model_exclude_unset=True)
    async def endpoint():
        return TreeNodeWithValidationAlias.model_validate({"vv": 1, "kids": {"a": [{"vv": 2}]}})

    clients = create_versioned_clients(version_change())

    # The values are lost, but the response passes validation. We only rescue responses that fail validation,
    # so the responses that work today never change.
    for version_client in clients.values():
        assert version_client.get(test_path).json() == {"kids": {"a": [{}]}}


class TreeWithValidationAlias(BaseModel):
    tree: TreeNodeWithValidationAlias = Field(alias="t", validation_alias="tt")


def test__response_migration__with_self_referencing_response_model_that_fails_validation__client_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=TreeWithValidationAlias, response_model_exclude_unset=True)
    async def endpoint():
        return TreeWithValidationAlias.model_validate(
            {
                "tt": {
                    "vv": 1,
                    "kids": {"a": [{"vv": 2}], "b": [{"vv": 3}], "unset": [{}], "without_values": [{"kids": {}}]},
                }
            }
        )

    clients = create_versioned_clients(version_change())

    # The required tree is missing without its validation alias, so we rescue the response and restore all of it
    for version_client in clients.values():
        assert version_client.get(test_path).json() == {
            "t": {"x": 1, "kids": {"a": [{"x": 2}], "b": [{"x": 3}], "unset": [{}], "without_values": [{"kids": {}}]}}
        }


class SchemaWithAliasChangedInCoreSchema(BaseModel):
    value: int = Field(default=0, validation_alias="v", serialization_alias="s")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        schema = handler(source)
        schema["schema"]["fields"]["value"]["validation_alias"] = "s"
        return schema


class SchemaWithSiblingAliasChangedInCoreSchema(BaseModel):
    first: int = Field(default=0, validation_alias="v", serialization_alias="s")
    second: int = Field(default=0, validation_alias="other")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        schema = handler(source)
        schema["schema"]["fields"]["second"]["validation_alias"] = "s"
        return schema


class SchemaWithValidationSettingsChangedInCoreSchema(BaseModel):
    value: int = Field(default=0, validation_alias="v")

    @override
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        schema = handler(source)
        schema["config"]["validate_by_alias"] = False
        schema["config"]["validate_by_name"] = True
        return schema


class ParentWithComputedField(BaseModel):
    @computed_field(alias="wire")
    @property
    def display(self) -> str:
        return "redacted"


SchemaWithFieldNamedLikeAComputedFieldOwner = create_model(
    "SchemaWithFieldNamedLikeAComputedFieldOwner",
    __base__=ParentWithComputedField,
    **{"computed field display": (int, Field(default=0, validation_alias="v", serialization_alias="wire"))},
)


@pytest.mark.parametrize(
    ("model", "payload", "read", "expected"),
    [
        pytest.param(SchemaWithAliasChangedInCoreSchema, {"v": 7}, lambda b: b.value, 7, id="alias_in_core_schema"),
        pytest.param(
            SchemaWithSiblingAliasChangedInCoreSchema,
            {"v": 7, "other": 7, "s": 7},
            lambda b: b.second,
            7,
            id="sibling_alias_in_core_schema",
        ),
        pytest.param(
            SchemaWithValidationSettingsChangedInCoreSchema,
            {"v": 7, "value": 7},
            lambda b: b.value,
            7,
            id="validation_settings_in_core_schema",
        ),
        pytest.param(
            SchemaWithFieldNamedLikeAComputedFieldOwner,
            {"v": 5},
            lambda b: getattr(b, "computed field display"),
            0,
            id="field_named_like_computed_field_owner",
        ),
    ],
)
def test__request_body_migration__with_keys_that_field_info_does_not_describe__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    payload: dict[str, Any],
    read: Callable[[Any], Any],
    expected: Any,
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return read(body)

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == expected


def test__request_body_migration__with_bulk_items_with_several_nested_models__head_should_get_the_values(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class Details(BaseModel):
        id: int
        value: int = Field(default=0, validation_alias="v")

    class Item(BaseModel):
        first: Details
        second: Details

    class SchemaWithItems(BaseModel):
        items: list[Item]
        # Required, so that the head surely rejects the body as it is and we restore all of it
        name: str = Field(validation_alias="n")

    @router.post(test_path)
    async def endpoint(body: SchemaWithItems):
        return body.model_dump()

    clients = create_versioned_clients(version_change())
    payload = {
        "items": [{"first": {"id": 1}, "second": {"id": 2, "v": 3}}, {"first": {"id": 4}, "second": {"id": 5}}],
        "n": "name",
    }

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == {
            "name": "name",
            "items": [
                {"first": {"id": 1, "value": 0}, "second": {"id": 2, "value": 3}},
                {"first": {"id": 4, "value": 0}, "second": {"id": 5, "value": 0}},
            ],
        }


def test__request_body_migration__with_safe_model_next_to_unsafe_one__should_restore_the_safe_one(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SafeInner(BaseModel):
        q: int = 0

    class WrapperWithUnsafeModel(BaseModel):
        money: MoneyWithModelSerializer = Field(default_factory=MoneyWithModelSerializer)
        safe: SafeInner = Field(default_factory=SafeInner)

    class WrapperWithSafeModel(BaseModel):
        safe: SafeInner = Field(default_factory=SafeInner)

    class SchemaWithWrappers(BaseModel):
        unsafe_wrapper: WrapperWithUnsafeModel = Field(default_factory=WrapperWithUnsafeModel, validation_alias="u")
        # Required, so that the head surely rejects the body as it is and we restore it
        safe_wrapper: WrapperWithSafeModel = Field(validation_alias="s")

    @router.post(test_path)
    async def endpoint(body: SchemaWithWrappers):
        return [body.unsafe_wrapper.safe.q, body.safe_wrapper.safe.q]

    clients = create_versioned_clients(version_change())
    payload = {"u": {"safe": {"q": 1}}, "s": {"safe": {"q": 2}}}

    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == [0, 2]


def test__request_body_migration__with_json_form_field__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: Annotated[SchemaWithJson, Form()]):
        return body.value

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, data={"v": "[1,2]"}).json() == []


class OptionalOuterWithRequiredInner(BaseModel):
    inner: Optional[InnerSchemaWithValidationAlias] = Field(default=None, alias="i", validation_alias="j")
    name: str = "n"


class OptionalOuterListWithRequiredInner(BaseModel):
    items: list[InnerSchemaWithValidationAlias] = Field(default=[], alias="i", validation_alias="j")


@pytest.mark.parametrize(
    ("model", "payload", "expected"),
    [
        pytest.param(OptionalOuterWithRequiredInner, {"j": {"v": 5}}, {"inner": None, "name": "n"}, id="nested_model"),
        pytest.param(OptionalOuterListWithRequiredInner, {"j": [{"v": 5}]}, {"items": []}, id="nested_list"),
    ],
)
def test__request_body_migration__with_required_field_under_optional_field_with_validation_alias__should_stay(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    payload: dict[str, Any],
    expected: dict[str, Any],
):
    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    # The head doesn't read the dumped key of the optional field, so it never validates the required field under it,
    # and the body passes as it is. We leave it as it is, so the optional field gets its default as before.
    for version_client in clients.values():
        assert version_client.post(test_path, json=payload).json() == expected


class OuterWithShadowedRequiredInner(BaseModel):
    # The dumped key "inner" is validated, but only if the higher-priority "j" is missing
    inner: InnerSchemaWithValidationAlias = Field(validation_alias=AliasChoices("j", "inner"))
    optional: int = Field(default=0, alias="o", validation_alias="ov")


def test__request_body_migration__with_required_field_under_shadowed_validation_key__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: OuterWithShadowedRequiredInner):
        return body.model_dump()

    @convert_request_to_next_version_for(OuterWithShadowedRequiredInner)
    def migrator(request: RequestInfo):
        request.body["j"] = {"v": 1}

    clients = create_versioned_clients(version_change(migrator=migrator))

    # The migration sets the key that the head reads instead of the dumped one, so the head never validates
    # the required field under the dumped key and the body passes as it is. We leave it as it is,
    # so the optional field gets its default as before.
    assert clients["2000-01-01"].post(test_path, json={"inner": {"v": 2}, "ov": 5}).json() == {
        "inner": {"value": 1},
        "optional": 0,
    }


@pytest.mark.parametrize(
    ("response_model", "response", "expected"),
    [
        pytest.param(
            OptionalOuterWithRequiredInner,
            {"j": {"v": 5}},
            {"i": None, "name": "n"},
            id="nested_model",
        ),
        pytest.param(OptionalOuterListWithRequiredInner, {"j": [{"v": 5}]}, {"i": []}, id="nested_list"),
    ],
)
def test__response_migration__with_required_field_under_optional_field_with_validation_alias__should_stay(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    response_model: type[BaseModel],
    response: dict[str, Any],
    expected: dict[str, Any],
):
    @router.get(test_path, response_model=response_model)
    async def endpoint():
        return response_model.model_validate(response)

    clients = create_versioned_clients(version_change())

    # The response model doesn't read the dumped key of the optional field, so it never validates the required field
    # under it, and the response passes as it is. We leave it as it is, so the optional field gets its default.
    for version_client in clients.values():
        assert version_client.get(test_path).json() == expected


def test__response_migration__with_required_field_under_shadowed_validation_key__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.get(test_path, response_model=OuterWithShadowedRequiredInner)
    async def endpoint():
        return OuterWithShadowedRequiredInner.model_validate({"inner": {"v": 2}, "ov": 5})

    @convert_response_to_previous_version_for(OuterWithShadowedRequiredInner)
    def migrator(response: ResponseInfo):
        response.body["j"] = {"v": 1}

    clients = create_versioned_clients(version_change(migrator=migrator))

    # The migration sets the key that the response model reads instead of the dumped one, so it never validates
    # the required field under the dumped key and the response passes as it is. We leave it as it is,
    # so the optional field gets its default as before.
    assert clients["2000-01-01"].get(test_path).json() == {"inner": {"x": 1}, "o": 0}


def test__request_body_migration__with_optional_field_with_validation_alias__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class SchemaWithStricterHead(BaseModel):
        value: int = Field(default=0, validation_alias="v", ge=10)

    @router.post(test_path)
    async def endpoint(body: SchemaWithStricterHead):
        return body.value

    clients = create_versioned_clients(version_change(schema(SchemaWithStricterHead).field("value").had(ge=0)))

    # The head accepts the body without the value, so we leave it as it is and the field gets its default as before,
    # even if the head would accept the value too. We only restore bodies that the head surely rejects as they are.
    assert clients["2000-01-01"].post(test_path, json={"v": 5}).json() == 0
    assert clients["2000-01-01"].post(test_path, json={"v": 15}).json() == 0
    assert clients["2001-01-01"].post(test_path, json={"v": 15}).json() == 0


def test__request_body_migration__with_value_that_the_head_rejects__should_run_dependencies_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    calls: list[str] = []

    def dependency() -> None:
        calls.append("dependency")

    def uncached_dependency() -> None:
        calls.append("uncached_dependency")

    def yield_dependency() -> Iterator[None]:
        calls.append("enter")
        yield
        calls.append("exit")

    def dependency_with_background_task(background_tasks: BackgroundTasks) -> None:
        background_tasks.add_task(calls.append, "background_task")

    class SchemaWithStricterHead(BaseModel):
        value: int = Field(alias="x", validation_alias="v", ge=10)

    @router.post(
        test_path,
        dependencies=[
            Depends(dependency),
            Depends(uncached_dependency, use_cache=False),
            Depends(yield_dependency),
            Depends(dependency_with_background_task),
        ],
    )
    async def endpoint(body: SchemaWithStricterHead):
        return body.value

    clients = create_versioned_clients(version_change(schema(SchemaWithStricterHead).field("value").had(ge=0)))

    # The dependencies run as many times as they did before: once for the version and once for the head
    assert clients["2000-01-01"].post(test_path, json={"v": 15}).json() == 15
    assert sorted(calls) == sorted(["dependency", "uncached_dependency", "enter", "exit"] * 2 + ["background_task"])

    # The head rejects the restored body, so we solve the request again with the body as it is to fail as before.
    # The second solve reuses the dependencies of the first one.
    calls.clear()
    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"v": 5})
    assert [error["type"] for error in exc_info.value.errors] == ["missing"]
    assert sorted(call for call in calls if call != "exit") == sorted(
        ["dependency", "uncached_dependency", "enter"] * 2
    )


def test__request_body_migration__with_form_value_that_the_head_rejects__should_run_dependencies_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    solvers: list[str] = []

    async def dependency(solver: str = Depends(current_dependency_solver)) -> None:
        solvers.append(solver)

    class FormWithStricterHead(BaseModel):
        value: int = Field(validation_alias="v", serialization_alias="x", ge=10)

    @router.post(test_path, dependencies=[Depends(dependency)])
    async def endpoint(body: Annotated[FormWithStricterHead, Form()]):
        return body.value

    clients = create_versioned_clients(version_change(schema(FormWithStricterHead).field("value").had(ge=0)))

    assert clients["2000-01-01"].post(test_path, data={"v": "15"}).json() == 15
    assert solvers == ["fastapi", "cadwyn"]

    # The head rejects the restored form, so we solve the request again with the form as it is to fail as before,
    # reusing the dependencies of the first solve
    solvers.clear()
    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, data={"v": "5"})
    assert [(error["type"], error["loc"]) for error in exc_info.value.errors] == [("missing", ("body", "v"))]
    assert solvers == ["fastapi", "cadwyn"]


def test__request_body_migration__with_required_value_that_the_head_rejects__should_fail_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    class RequiredValueWithStricterHead(BaseModel):
        value: int = Field(validation_alias="v", ge=10)

    @router.post(test_path)
    async def endpoint(body: RequiredValueWithStricterHead):
        raise NotImplementedError  # pragma: no cover

    clients = create_versioned_clients(version_change(schema(RequiredValueWithStricterHead).field("value").had(ge=0)))

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"v": 5})
    # The error is the one that the body would get without the value, as before
    assert [error["type"] for error in exc_info.value.errors] == ["missing"]


@pytest.mark.parametrize("fails_on_third_validation", [False, True], ids=["counts", "rejects_third_validation"])
def test__request_body_migration__with_validation_aliases__should_validate_the_body_as_many_times_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    fails_on_third_validation: bool,
):
    validations: list[str] = []

    class SchemaWithStatefulValidator(BaseModel):
        value: int = Field(alias="x", validation_alias="v")
        data: Any

        @model_validator(mode="after")
        def count_validations(self) -> Self:
            validations.append("validate")
            if fails_on_third_validation and len(validations) > 2:
                raise ValueError("Already validated twice")  # pragma: no cover # Only if the test fails
            self.data["count"] += 1
            return self

    @router.post(test_path)
    async def endpoint(body: SchemaWithStatefulValidator):
        return {"value": body.value, "count": body.data["count"]}

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        validations.clear()
        # The version validates the body once, and the head validates the restored body once
        assert version_client.post(test_path, json={"v": 7, "data": {"count": 0}}).json() == {"value": 7, "count": 2}
        assert len(validations) == 2


_head_dependency_has_run: ContextVar[bool] = ContextVar("_head_dependency_has_run", default=False)


async def _set_up_head_validation(solver: str = Depends(current_dependency_solver)) -> None:
    _head_dependency_has_run.set(solver == "cadwyn")


class SchemaValidatedAfterHeadDependency(BaseModel):
    value: int = Field(alias="x", validation_alias="v")

    @model_validator(mode="after")
    def check_that_head_dependency_has_run(self) -> Self:
        if type(self) is SchemaValidatedAfterHeadDependency and self.value and not _head_dependency_has_run.get():
            raise ValueError("The head dependency hasn't run yet")  # pragma: no cover # Only if the test fails
        return self


def test__request_body_migration__with_validators_that_rely_on_head_dependencies__head_should_get_the_value(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, dependencies=[Depends(_set_up_head_validation)])
    async def endpoint(body: SchemaValidatedAfterHeadDependency):
        return body.value

    clients = create_versioned_clients(version_change())

    # The head validates the restored body while solving its dependencies, like any other body
    for version_client in clients.values():
        assert version_client.post(test_path, json={"v": 7}).json() == 7


class SchemaWithAliasPathsIntoAnotherField(BaseModel):
    names: list[str]
    first_name: Annotated[str, AfterValidator(str.upper)] = Field(validation_alias=AliasPath("names", 0))
    last_name: Annotated[str, AfterValidator(str.upper)] = Field(validation_alias=AliasPath("names", -1))


def test__request_body_migration__with_alias_paths_into_another_field__should_not_overwrite_it(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithAliasPathsIntoAnotherField):
        return body.model_dump()

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        assert version_client.post(test_path, json={"names": ["John", "Doe"]}).json() == {
            "names": ["John", "Doe"],
            "first_name": "JOHN",
            "last_name": "DOE",
        }


class SchemaWithValidationAliasThatChangedItsType(BaseModel):
    value: int = Field(alias="x", validation_alias="v")
    other: int = Field(default=0, alias="y", validation_alias="w")


def test__request_body_migration__that_converts_the_value_of_a_field_with_validation_alias__head_should_get_it(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithValidationAliasThatChangedItsType):
        return body.model_dump()

    @convert_request_to_next_version_for(SchemaWithValidationAliasThatChangedItsType)
    def migrator(request: RequestInfo):
        request.body["x"] = len(request.body["x"])

    clients = create_versioned_clients(
        version_change(
            schema(SchemaWithValidationAliasThatChangedItsType).field("value").had(type=str),
            migrator=migrator,
        )
    )

    assert clients["2000-01-01"].post(test_path, json={"v": "abc", "w": 2}).json() == {"value": 3, "other": 2}
    assert clients["2001-01-01"].post(test_path, json={"v": 3, "w": 2}).json() == {"value": 3, "other": 2}


def test__request_body_migration__that_sets_the_validation_alias_itself__head_should_get_its_value_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithValidationAliasThatChangedItsType):
        return body.model_dump()

    @convert_request_to_next_version_for(SchemaWithValidationAliasThatChangedItsType)
    def migrator(request: RequestInfo):
        request.body["v"] = request.body["x"] * 10
        request.body["w"] = request.body.pop("y") * 10

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json={"v": 1, "w": 2}).json() == {"value": 10, "other": 20}
    assert clients["2001-01-01"].post(test_path, json={"v": 1, "w": 2}).json() == {"value": 1, "other": 2}


def test__request_and_response_migrations__for_paths_with_variables__can_match(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test/{id}")
    async def endpoint(id: int, my_query: str = fastapi.Query(default="wow")):
        return [id, my_query]

    @convert_request_to_next_version_for("/test/{id}", ["POST"])
    def request_converter(request: RequestInfo):
        request.query_params["my_query"] = "Hewwo"

    @convert_response_to_previous_version_for("/test/{id}", ["POST"])
    def response_converter(response: ResponseInfo):
        response.body.append("World")

    clients = create_versioned_clients(version_change(req=request_converter, resp=response_converter))
    assert clients["2000-01-01"].post("/test/83").json() == [83, "Hewwo", "World"]
    assert clients["2001-01-01"].post("/test/83").json() == [83, "wow"]


def test__request_and_response_migrations__for_endpoint_with_http_exception__can_migrate_to_200(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test")
    async def endpoint():
        raise HTTPException(status_code=404)

    @convert_response_to_previous_version_for("/test", ["POST"], migrate_http_errors=True)
    def response_converter(response: ResponseInfo):
        response.status_code = 200
        response.body = {"hello": "darkness"}
        response.headers["hewwo"] = "dawkness"

    clients = create_versioned_clients(version_change(resp=response_converter))
    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 200
    assert resp_2000.json() == {"hello": "darkness"}
    assert resp_2000.headers["hewwo"] == "dawkness"

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 404
    assert resp_2001.json() == {"detail": "Not Found"}
    assert "hewwo" not in resp_2001.headers


def test__request_and_response_migrations__for_endpoint_with_http_exception_and_no_error_migrations__wont_migrate(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test")
    async def endpoint():
        raise HTTPException(status_code=400)

    @convert_response_to_previous_version_for("/test", ["POST"])
    def response_converter(response: ResponseInfo):
        raise NotImplementedError("This should not be called")

    clients = create_versioned_clients(version_change(resp=response_converter))
    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 400

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 400


def test__request_and_response_migrations__for_endpoint_with_http_exception__can_migrate_to_another_error(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test")
    async def endpoint():
        raise HTTPException(status_code=404)

    @convert_response_to_previous_version_for("/test", ["POST"], migrate_http_errors=True)
    def response_converter(response: ResponseInfo):
        response.status_code = 401
        response.body = None

    clients = create_versioned_clients(version_change(resp=response_converter))
    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 401
    assert resp_2000.json() == {"detail": "Unauthorized"}

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 404
    assert resp_2001.json() == {"detail": "Not Found"}


def test__request_and_response_migrations__for_endpoint_with_no_default_status_code__response_should_contain_default(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test")
    async def endpoint():
        return 83

    @convert_response_to_previous_version_for("/test", ["POST"])
    def response_converter(response: ResponseInfo):
        assert response.status_code == 200

    clients = create_versioned_clients(version_change(resp=response_converter))

    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 200
    assert resp_2000.json() == 83

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 200
    assert resp_2001.json() == 83


def test__request_and_response_migrations__for_endpoint_with_custom_status_code__response_should_contain_default(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test", status_code=201)
    async def endpoint():
        return 83

    @convert_response_to_previous_version_for("/test", ["POST"])
    def response_converter(response: ResponseInfo):
        assert response.status_code == 201

    clients = create_versioned_clients(version_change(resp=response_converter))

    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 201
    assert resp_2000.json() == 83

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 201
    assert resp_2001.json() == 83


def test__request_and_response_migrations__for_endpoint_with_modified_status_code__response_should_not_change(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test")
    async def endpoint(response: Response):
        response.status_code = 201
        return 83

    @convert_response_to_previous_version_for("/test", ["POST"])
    def response_converter(response: ResponseInfo):
        assert response.status_code == 201

    clients = create_versioned_clients(version_change(resp=response_converter))

    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 201
    assert resp_2000.json() == 83

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 201
    assert resp_2001.json() == 83


def test__response_migrations__with_manual_string_json_response_and_migration(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test")
    async def endpoint():
        return JSONResponse(content="My content")

    @convert_response_to_previous_version_for("/test", ["POST"])
    def response_converter(response: ResponseInfo):
        pass

    clients = create_versioned_clients(version_change(resp=response_converter))

    resp_2000 = clients["2000-01-01"].post("/test")
    assert resp_2000.status_code == 200
    assert resp_2000.json() == "My content"

    resp_2001 = clients["2001-01-01"].post("/test")
    assert resp_2001.status_code == 200
    assert resp_2001.json() == "My content"


@pytest.mark.parametrize(("path", "method"), [("/NOT_test", "POST"), ("/test", "PUT")])
def test__request_by_path_migration__for_nonexistent_endpoint_path__should_raise_error(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
    path: str,
    method: str,
):
    @router.post("/test")
    async def endpoint():
        raise NotImplementedError

    @convert_request_to_next_version_for(path, [method])
    def request_converter(request: RequestInfo):
        raise NotImplementedError

    with pytest.raises(RouteByPathConverterDoesNotApplyToAnythingError):
        create_versioned_clients(version_change(converter=request_converter))


@pytest.mark.parametrize(("path", "method"), [("/NOT_test", "POST"), ("/test", "PUT")])
def test__response_by_path_migration__for_nonexistent_endpoint_path__should_raise_error(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
    path: str,
    method: str,
):
    @router.post("/test")
    async def endpoint():
        raise NotImplementedError

    @convert_response_to_previous_version_for(path, [method])
    def response_converter(response: ResponseInfo):
        raise NotImplementedError

    with pytest.raises(RouteByPathConverterDoesNotApplyToAnythingError):
        create_versioned_clients(version_change(converter=response_converter))


def test__request_by_schema_migration__for_nonexistent_schema__should_raise_error(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test", response_model=AnyResponseSchema)
    async def endpoint(body: AnyRequestSchema):
        raise NotImplementedError

    # Using response model for requests to cause an error
    @convert_request_to_next_version_for(AnyResponseSchema)
    def request_converter(request: RequestInfo):
        raise NotImplementedError

    with pytest.raises(RouteRequestBySchemaConverterDoesNotApplyToAnythingError):
        create_versioned_clients(version_change(converter=request_converter))


def test__response_by_schema_migration__for_nonexistent_schema__should_raise_error(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    @router.post("/test", response_model=AnyResponseSchema)
    async def endpoint(body: AnyRequestSchema):
        raise NotImplementedError

    # Using request model for responses to cause an error
    @convert_response_to_previous_version_for(AnyRequestSchema)
    def response_converter(response: ResponseInfo):
        raise NotImplementedError

    with pytest.raises(RouteResponseBySchemaConverterDoesNotApplyToAnythingError):
        create_versioned_clients(version_change(converter=response_converter))


def test__manual_response_migrations():
    @convert_response_to_previous_version_for(EmptySchema)
    def response_converter(response: ResponseInfo):
        response.body["amount"] = 83

    version_bundle = VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                schema(EmptySchema).field("name").existed_as(type=str, info=Field(default="Apples")),
                schema(EmptySchema).field("amount").existed_as(type=int),
                convert=response_converter,
            ),
        ),
        Version("2000-01-01"),
    )

    new_response = migrate_response_body(version_bundle, EmptySchema, latest_body={"id": "hewwo"}, version="2000-01-01")
    assert new_response.model_dump() == {
        "name": "Apples",
        "amount": 83,
    }
    assert new_response.model_dump(exclude_unset=True) == {"amount": 83}

    with pytest.raises(CadwynError):
        new_response = migrate_response_body(
            version_bundle, EmptySchema, latest_body={"id": "hewwo"}, version="1999-01-01"
        )


@pytest.mark.parametrize("version", ["2000-01-01", "2001-01-01"])
@pytest.mark.parametrize(
    "latest_body",
    [
        pytest.param({"i": {"x": 1}, "items": [{"x": 2}]}, id="dumped_keys"),
        pytest.param({"j": {"v": 1}, "items": [{"v": 2}]}, id="validation_keys"),
    ],
)
def test__manual_response_migrations__with_validation_aliases__should_get_the_values(
    version: str, latest_body: dict[str, Any]
):
    @convert_response_to_previous_version_for(OuterSchemaWithValidationAliases)
    def response_converter(response: ResponseInfo):
        pass

    version_bundle = VersionBundle(
        Version("2001-01-01", version_change(convert=response_converter)), Version("2000-01-01")
    )

    migrated = migrate_response_body(
        version_bundle, OuterSchemaWithValidationAliases, latest_body=latest_body, version=version
    )

    assert migrated.model_dump(by_alias=True, exclude_unset=True) == {"i": {"x": 1}, "items": [{"x": 2}]}


@pytest.mark.parametrize("version", ["2000-01-01", "2001-01-01"])
def test__manual_response_migrations__with_validation_aliases_that_pass_validation__should_stay_as_before(
    version: str,
):
    version_bundle = VersionBundle(Version("2001-01-01"), Version("2000-01-01"))

    migrated = migrate_response_body(
        version_bundle, SchemaWithDefaultAndValidationAlias, latest_body={"x": 5}, version=version
    )

    # The body passes validation with the default, so we keep it as it is
    assert migrated.model_dump(by_alias=True) == {"x": 0}


@pytest.mark.parametrize(
    "latest_body",
    [pytest.param({}, id="nothing_to_restore"), pytest.param({"x": "not an int"}, id="invalid_restored_value")],
)
def test__manual_response_migrations__with_body_that_fails_with_validation_aliases__should_fail_as_before(
    latest_body: dict[str, Any],
):
    version_bundle = VersionBundle(Version("2001-01-01"), Version("2000-01-01"))

    with pytest.raises(ValidationError) as exc_info:
        migrate_response_body(
            version_bundle, InnerSchemaWithValidationAlias, latest_body=latest_body, version="2000-01-01"
        )
    assert [error["type"] for error in exc_info.value.errors()] == ["missing"]


def test__manual_response_migrations__with_rescue_that_raises__should_fail_with_the_original_error():
    version_bundle = VersionBundle(Version("2001-01-01"), Version("2000-01-01"))

    with pytest.raises(ValidationError) as exc_info:
        migrate_response_body(
            version_bundle, SchemaWithValidatorThatRaisesTypeError, latest_body={"x": -1}, version="2000-01-01"
        )
    assert [error["type"] for error in exc_info.value.errors()] == ["missing"]


def test__request_and_response_migrations__with_multiple_schemas_in_converters(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
) -> None:
    class Request1(BaseModel):
        i: list[str]

    class Response1(BaseModel):
        i: list[str]

    class Request2(BaseModel):
        i: list[str]

    class Response2(BaseModel):
        i: list[str]

    class Request3(BaseModel):
        i: list[str]

    class Response3(BaseModel):
        i: list[str]

    @router.post("/test_1", response_model=Response1)
    async def endpoint_1(body: Request1):
        body.i.append("test_1")
        return body

    @router.post("/test_2", response_model=Response2)
    async def endpoint_2(body: Request2):
        body.i.append("test_2")
        return body

    @router.post("/test_3", response_model=Response3)
    async def endpoint_3(body: Request3):
        body.i.append("test_3")
        return body

    @convert_request_to_next_version_for(Request1, Request2, Request3)
    def request_converter(request: RequestInfo):
        request.body["i"].append("request_migration")

    @convert_response_to_previous_version_for(Response1, Response2, Response3)
    def response_converter(response: ResponseInfo):
        response.body["i"].append("response_migration")

    clients = create_versioned_clients(version_change(req=request_converter, resp=response_converter))
    client_2000, client_2001 = clients.values()

    for endpoint in ("test_1", "test_2", "test_3"):
        resp_2000 = client_2000.post(f"/{endpoint}", json={"i": ["original_request"]})
        assert resp_2000.status_code == 200
        assert resp_2000.json() == {"i": ["original_request", "request_migration", endpoint, "response_migration"]}

        resp_2001 = client_2001.post(f"/{endpoint}", json={"i": ["original_request"]})
        assert resp_2001.status_code == 200
        assert resp_2001.json() == {"i": ["original_request", endpoint]}


def test__response_migrations__with_custom_http_exception(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
) -> None:
    class CustomHTTPException(HTTPException):
        error_code: Optional[str] = None

        def __init__(self, detail: str, error_code: Optional[str] = None):
            self.error_code = error_code
            super().__init__(status_code=400, detail=detail)

    def http_exception_handler(request, exc):
        # Check if the exception has an error_code attribute
        error_code = exc.error_code if hasattr(exc, "error_code") else "generic_error"

        return JSONResponse(
            status_code=exc.status_code,
            content={"code": error_code, "message": exc.detail},
        )

    # Register exception handler for Cadwyn

    @router.post("/test")
    async def endpoint():
        raise CustomHTTPException("Cadwyn error occurred", error_code="cadwyn_error")

    app = create_versioned_app(version_change())
    app.add_exception_handler(HTTPException, http_exception_handler)
    with TestClient(app) as client:
        resp = client.post("/test", headers={"X-API-VERSION": "2000-01-01"})
        assert resp.status_code == 400
        assert resp.json() == {"code": "cadwyn_error", "message": "Cadwyn error occurred"}

        resp_2001 = client.post("/test", headers={"X-API-VERSION": "2001-01-01"})
        assert resp_2001.status_code == 400
        assert resp_2001.json() == {"code": "cadwyn_error", "message": "Cadwyn error occurred"}
