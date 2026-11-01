import dataclasses
import http.cookies
import inspect
import itertools
import json
import pathlib
import re
import sys
import tempfile
import threading
import time as time_module
import typing
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Iterable, Iterator
from contextvars import ContextVar
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from io import BytesIO, RawIOBase, StringIO, UnsupportedOperation
from types import MappingProxyType
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
    StringConstraints,
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
    FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    FASTAPI_SUPPORTS_FORM_MODEL_UNIONS,
    FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS,
    FASTAPI_VALIDATES_FORM_FIELDS_OF_OVERRIDES,
    FASTAPI_VALIDATES_FORM_MODEL_UNIONS_AS_FORM_DATA,
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


async def _optional_form_model(body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None):
    return None if body is None else body.value


async def _optional_form_model_from_dependency(
    body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None,
) -> Optional[int]:
    return None if body is None else body.value


async def _optional_form_model_shared_by_dependency(
    body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None,
    from_dependency: Optional[int] = Depends(_optional_form_model_from_dependency),
):
    return [None if body is None else body.value, from_dependency]


@pytest.mark.parametrize(
    ("endpoint", "data"),
    [
        pytest.param(_optional_form_model, None, id="without_form_data"),
        pytest.param(_optional_form_model, {"unrelated": "1"}, id="with_unrelated_form_data"),
        pytest.param(_optional_form_model, {"v": "1", "unrelated": "1"}, id="with_its_form_data"),
        pytest.param(_optional_form_model_shared_by_dependency, None, id="shared_by_dependency_without_form_data"),
        pytest.param(
            _optional_form_model_shared_by_dependency, {"v": "1"}, id="shared_by_dependency_with_its_form_data"
        ),
    ],
)
def test__request_body_migration__with_optional_form_model__head_should_get_it_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    data: Optional[dict[str, str]],
):
    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change())

    expected = TestClient(fastapi_app).post(test_path, data=data)
    for version_client in clients.values():
        response = version_client.post(test_path, data=data)
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())


async def _get_current_user() -> str:
    raise NotImplementedError


async def _get_fake_user() -> str:
    return "fake"


async def _get_optional_form_model_value_in_override(
    body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None,
) -> Optional[int]:
    return None if body is None else body.value


async def _optional_form_model_next_to_overridden_dependency(
    body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None,
    from_dependency: Any = Depends(_get_current_user),
):
    return [None if body is None else body.value, from_dependency]


@pytest.mark.parametrize(
    ("override", "data"),
    [
        pytest.param(_get_fake_user, None, id="override_without_params_and_no_form_data"),
        pytest.param(_get_fake_user, {"unrelated": "1"}, id="override_without_params_and_unrelated_form_data"),
        pytest.param(
            _get_optional_form_model_value_in_override, None, id="override_sharing_form_model_and_no_form_data"
        ),
        pytest.param(
            _get_optional_form_model_value_in_override, {"v": "1"}, id="override_sharing_form_model_and_its_form_data"
        ),
    ],
)
def test__request_body_migration__with_optional_form_model_and_dependency_override__head_should_get_it_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    override: Callable[..., Coroutine[Any, Any, Any]],
    data: Optional[dict[str, str]],
):
    router.post(test_path)(_optional_form_model_next_to_overridden_dependency)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(_optional_form_model_next_to_overridden_dependency)
    fastapi_app.dependency_overrides[_get_current_user] = override
    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_current_user] = override

    expected = TestClient(fastapi_app).post(test_path, data=data)
    for version_client in clients.values():
        response = version_client.post(test_path, data=data)
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, migrations get the form of the request"
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply__migration_should_get_none(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    router.post(test_path)(_optional_form_model)
    bodies_seen_by_migration: list[Any] = []

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        bodies_seen_by_migration.append(request.body)

    clients = create_versioned_clients(version_change(migrator=migrator))

    response = clients["2000-01-01"].post(test_path, data={"unrelated": "1"})
    assert (response.status_code, response.json()) == (200, None)
    assert bodies_seen_by_migration == [None]


class _RequiredFormModelThatWrapsEmptyFormsAsNone(RootModel[Optional[dict[str, str]]]):
    @model_validator(mode="wrap")
    @classmethod
    def _empty_form_as_none(  # pragma: no cover # FastAPI >= 0.158.2
        cls, value: Any, handler: Callable[[Any], Any]
    ) -> Any:
        return None if value in ({}, None) else handler(value)


class _FormModelWithExtraFields(BaseModel):
    model_config = ConfigDict(extra="allow")
    value: Optional[int] = Field(default=None, validation_alias="v")


def _none_for_zero(  # pragma: no cover # FastAPI >= 0.158.2
    value: Optional[InnerSchemaWithValidationAlias],
) -> Optional[InnerSchemaWithValidationAlias]:
    return None if value is not None and value.value == 0 else value


def _none_without_value(  # pragma: no cover # FastAPI >= 0.158.2
    value: Optional[_FormModelWithExtraFields],
) -> Optional[_FormModelWithExtraFields]:
    return None if value is not None and value.value is None else value


async def _none_dependency() -> None:  # pragma: no cover # FastAPI >= 0.158.2
    return None


async def _get_extra_from_form(
    extra: Optional[str] = Form(None),
) -> Optional[str]:  # pragma: no cover # FastAPI >= 0.158.2
    return extra


def _add_required_form_model_that_validates_to_none_endpoint(  # pragma: no cover # FastAPI >= 0.158.2
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(body: Annotated[_RequiredFormModelThatWrapsEmptyFormsAsNone, Form()]):
        raise NotImplementedError


def _add_form_model_that_validates_to_none_with_non_none_default_endpoint(  # pragma: no cover # FastAPI >= 0.158.2
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(
        body: Annotated[Optional[InnerSchemaWithValidationAlias], Form(), AfterValidator(_none_for_zero)] = (
            InnerSchemaWithValidationAlias(v=1)
        ),
    ):
        raise NotImplementedError


def _add_form_model_with_extra_fields_that_validates_to_none_endpoint(  # pragma: no cover # FastAPI >= 0.158.2
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(
        body: Annotated[Optional[_FormModelWithExtraFields], Form(), AfterValidator(_none_without_value)] = None,
    ):
        raise NotImplementedError


class _FormModelWithDefaults(BaseModel):
    value: int = Field(default=0, validation_alias="v")


def _always_none(value: Any) -> None:  # pragma: no cover # FastAPI >= 0.158.2
    return None


def _add_non_nullable_form_model_that_validates_to_none_endpoint(  # pragma: no cover # FastAPI >= 0.158.2
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(
        body: Annotated[_FormModelWithDefaults, Form(), AfterValidator(_always_none)] = _FormModelWithDefaults(v=7),
    ):
        raise NotImplementedError


def _add_form_model_next_to_form_field_of_dependency_override_endpoint(  # pragma: no cover # FastAPI >= 0.158.2
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(
        body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None,
        extra: Optional[str] = Depends(_none_dependency),
    ):
        raise NotImplementedError


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, Cadwyn takes the same path as before"
)
@pytest.mark.parametrize(
    ("add_endpoint", "data", "dependency_overrides"),
    [
        pytest.param(_add_required_form_model_that_validates_to_none_endpoint, None, {}, id="required_form_model"),
        pytest.param(
            _add_form_model_that_validates_to_none_with_non_none_default_endpoint,
            {"v": "0"},
            {},
            id="form_model_with_its_form_data_and_non_none_default",
        ),
        pytest.param(
            _add_form_model_with_extra_fields_that_validates_to_none_endpoint,
            {"unrelated": "1"},
            {},
            id="form_model_with_extra_fields",
        ),
        pytest.param(
            _add_non_nullable_form_model_that_validates_to_none_endpoint,
            None,
            {},
            id="non_nullable_form_model_without_form_data",
        ),
        pytest.param(
            _add_non_nullable_form_model_that_validates_to_none_endpoint,
            {"unrelated": "1"},
            {},
            id="non_nullable_form_model_with_unrelated_form_data",
        ),
        pytest.param(
            _add_form_model_next_to_form_field_of_dependency_override_endpoint,
            {"extra": "1"},
            {_none_dependency: _get_extra_from_form},
            id="optional_form_model_next_to_form_field_of_dependency_override",
        ),
    ],
)
def test__request_body_migration__with_form_model_that_fastapi_validates_to_none__should_fail_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    add_endpoint: Callable[[VersionedAPIRouter, str], None],
    data: Optional[dict[str, str]],
    dependency_overrides: dict[Any, Any],
):
    add_endpoint(router, test_path)
    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides.update(dependency_overrides)

    # FastAPI has validated the form, so the head doesn't get an omitted body that would lose the form's values
    for version_client in clients.values():
        with pytest.raises(CadwynHeadRequestValidationError):
            version_client.post(test_path, data=data)


class _FormModelThatValidatesEmptyFormsAsNone(RootModel[Optional[dict[str, str]]]):
    @model_validator(mode="before")
    @classmethod
    def _empty_form_as_none(cls, value: Any) -> Any:  # pragma: no cover # FastAPI >= 0.151
        return None if value == {} else value


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.151
    not FASTAPI_DISTINGUISHES_NULL_BODIES, reason="Before FastAPI 0.151.0, a body that is None is an omitted one"
)
def test__request_body_migration__with_supplied_form_model_that_dumps_to_none__head_should_get_it_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: Annotated[_FormModelThatValidatesEmptyFormsAsNone, Form()]):
        return body.root

    clients = create_versioned_clients(version_change())

    for version_client in clients.values():
        response = version_client.post(test_path)
        assert (response.status_code, response.json()) == (200, None)


def _add_optional_file_next_to_query_param_named_body_endpoint(  # pragma: no cover # FastAPI >= 0.151
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(upload: Optional[bytes] = File(None), body: Optional[str] = Query(None)):
        raise NotImplementedError


def _add_optional_form_model_aliased_as_dependency_named_body_endpoint(  # pragma: no cover # FastAPI >= 0.158.2
    router: VersionedAPIRouter, path: str
) -> None:
    @router.post(path)
    async def endpoint(
        payload: Annotated[Optional[InnerSchemaWithValidationAlias], Form(alias="body")] = None,
        body: None = Depends(_none_dependency),
    ):
        raise NotImplementedError


@pytest.mark.parametrize(  # pragma: no cover # FastAPI >= 0.151
    ("add_endpoint", "request_kwargs"),
    [
        pytest.param(
            _add_optional_file_next_to_query_param_named_body_endpoint,
            {"files": {"upload": ("file.txt", b"content")}},
            id="optional_file_next_to_query_param_named_body",
            marks=pytest.mark.skipif(
                not FASTAPI_DISTINGUISHES_NULL_BODIES,
                reason="Before FastAPI 0.151.0, a body that is None is an omitted one",
            ),
        ),
        pytest.param(
            _add_optional_form_model_aliased_as_dependency_named_body_endpoint,
            {"data": {"v": "1"}},
            id="optional_form_model_aliased_as_dependency_named_body",
            marks=pytest.mark.skipif(
                not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS,
                reason="Before FastAPI 0.158.2, FastAPI embeds `Optional` form models",
            ),
        ),
    ],
)
def test__request_body_migration__with_none_kwarg_named_body_that_is_not_the_body__should_not_drop_the_body(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    add_endpoint: Callable[[VersionedAPIRouter, str], None],
    request_kwargs: dict[str, Any],
):
    add_endpoint(router, test_path)
    clients = create_versioned_clients(version_change())

    # Cadwyn takes the kwarg for the body, so it fails as before instead of passing the head no body
    for version_client in clients.values():
        with pytest.raises(CadwynHeadRequestValidationError):
            version_client.post(test_path, **request_kwargs)


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


class SchemaWithText(BaseModel):
    text: str


async def _get_text() -> str:
    raise NotImplementedError


async def _get_text_from_form_field(text: str = Form()) -> str:
    return text  # pragma: no cover # FastAPI >= 0.158


@pytest.mark.parametrize(
    "form_model_param",
    [Form(), Form(validation_alias=AliasChoices("other", "text"))],
    ids=["name", "alias_choices"],
)
def test__request_body_migration__with_form_field_of_dependency_override_named_like_form_model__head_should_get_both(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    form_model_param: Any,
):
    @router.post(test_path)
    async def endpoint(text: Annotated[SchemaWithText, form_model_param], from_dependency: str = Depends(_get_text)):
        return [text.text, from_dependency]  # pragma: no cover # FastAPI >= 0.158

    @convert_request_to_next_version_for(SchemaWithText)
    def migrator(request: RequestInfo):
        request.body["text"] = "migrated"  # pragma: no cover # FastAPI >= 0.158

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_text] = _get_text_from_form_field

    error = {"type": "string_type", "loc": ["body"], "msg": "Input should be a valid string", "input": {"text": "sent"}}
    assert {version: client.post(test_path, data={"text": "sent"}).json() for version, client in clients.items()} == (
        {"2000-01-01": ["migrated", "migrated"], "2001-01-01": ["sent", "sent"]}
        if FASTAPI_VALIDATES_FORM_FIELDS_OF_OVERRIDES
        else {"2000-01-01": {"detail": [error]}, "2001-01-01": {"detail": [error]}}
    )


async def _get_text_from_embedded_body(text: str = Body(embed=True)) -> str:
    return text  # pragma: no cover # FastAPI >= 0.158


def test__request_body_migration__with_embedded_body_param_of_override_named_like_form_model__head_should_get_both(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(text: Annotated[SchemaWithText, Form()], from_dependency: str = Depends(_get_text)):
        return [text.text, from_dependency]  # pragma: no cover # FastAPI >= 0.158

    @convert_request_to_next_version_for(SchemaWithText)
    def migrator(request: RequestInfo):
        request.body["text"] = "migrated"  # pragma: no cover # FastAPI >= 0.158

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_text] = _get_text_from_embedded_body

    error = {"type": "string_type", "loc": ["body"], "msg": "Input should be a valid string", "input": {"text": "sent"}}
    assert {version: client.post(test_path, data={"text": "sent"}).json() for version, client in clients.items()} == (
        {"2000-01-01": ["migrated", "migrated"], "2001-01-01": ["sent", "sent"]}
        if FASTAPI_VALIDATES_FORM_FIELDS_OF_OVERRIDES
        else {"2000-01-01": {"detail": [error]}, "2001-01-01": {"detail": [error]}}
    )


class OtherSchemaWithText(BaseModel):
    other_text: str


async def _get_text_from_form_model_union(
    text: Annotated[Union[SchemaWithText, OtherSchemaWithText], Form()],
) -> dict[str, str]:
    return text.model_dump()


@pytest.mark.skipif(
    FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS and not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS,
    reason="On FastAPI 0.158.2 to 0.160, a union of form models isn't a form model, so it can't get the dict body",
)
def test__request_body_migration__with_form_model_union_of_override_named_like_form_model__head_should_get_it(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(text: Annotated[SchemaWithText, Form()], from_dependency: Any = Depends(_get_text)):
        return [text.text, from_dependency]

    @convert_request_to_next_version_for(SchemaWithText)
    def migrator(request: RequestInfo):
        request.body["text"] = "migrated"

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_text] = _get_text_from_form_model_union

    assert {version: client.post(test_path, data={"text": "sent"}).json() for version, client in clients.items()} == {
        "2000-01-01": ["migrated", {"text": "migrated"}],
        "2001-01-01": ["sent", {"text": "sent"}],
    }


async def _form_model_union(text: Annotated[Union[SchemaWithText, OtherSchemaWithText], Form()]):
    return text.model_dump()


async def _optional_form_model_union(
    text: Annotated[Optional[Union[SchemaWithText, OtherSchemaWithText]], Form()] = None,
):
    return None if text is None else text.model_dump()


class _FormModelWithFile(BaseModel):
    value: str
    file: Optional[UploadFile] = None


async def _optional_form_model_with_file(  # pragma: no cover # FastAPI >= 0.158.2
    body: Annotated[Optional[_FormModelWithFile], Form()] = None,
):
    return None if body is None else [body.value, body.file is None]


_SKIP_IF_FORM_MODEL_UNIONS_ARE_NOT_FORM_MODELS = pytest.mark.skipif(
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS, reason="Before FastAPI 0.161.0, a union of form models isn't a form model"
)

# Since FastAPI 0.153.0, FastAPI validates the members of a union of form models from the body as if it were form data
_SKIP_IF_CADWYN_PASSES_FORM_MODEL_UNION_DUMPS_AS_THEY_ARE = pytest.mark.skipif(
    FASTAPI_VALIDATES_FORM_MODEL_UNIONS_AS_FORM_DATA and not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS,
    reason="On FastAPI 0.153 to 0.160, Cadwyn passes the dump of the union member to FastAPI as it is",
)


class SchemaWithCollections(BaseModel):
    tags: set[str]
    frozen_tags: frozenset[str]
    numbers: tuple[int, ...]


async def _form_model_union_with_collections(
    body: Annotated[Union[SchemaWithCollections, OtherSchemaWithText], Form()],
):
    return body.model_dump(mode="json")


def _split_by_commas(value: Any) -> Any:
    return value.split(",") if isinstance(value, str) else value  # pragma: no cover # FastAPI >= 0.161


class SchemaWithSplitValue(BaseModel):
    value: Annotated[Any, BeforeValidator(_split_by_commas)]


# The head rejects the bodies that these endpoints get in the tests
async def _form_model_union_with_split_value(
    body: Annotated[Union[SchemaWithSplitValue, OtherSchemaWithText], Form()],
):
    return body.model_dump()  # pragma: no cover


class SchemaWithListValue(BaseModel):
    kind: Literal["list"]
    value: list[str]


class SchemaWithSplitValueOfKind(SchemaWithSplitValue):
    kind: Literal["split"]


async def _form_model_union_with_list_and_split_value(
    body: Annotated[Union[SchemaWithListValue, SchemaWithSplitValueOfKind], Form()],
):
    return body.model_dump()  # pragma: no cover


class SchemaWithExtraValue(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["extra"]

    @model_validator(mode="before")
    @classmethod
    def split_value(cls, data: Any) -> Any:  # pragma: no cover # FastAPI >= 0.161
        value = data.get("value") if isinstance(data, dict) else None
        if isinstance(value, str):
            return {**data, "value": tuple(value.split(","))}
        if isinstance(value, list):
            return {**data, "value": f"repeated:{','.join(value)}"}
        return data


class SchemaWithTupleValue(BaseModel):
    kind: Literal["tuple"]
    value: tuple[str, ...]


async def _form_model_union_with_extra_and_tuple_value(
    body: Annotated[Union[SchemaWithExtraValue, SchemaWithTupleValue], Form()],
):
    return body.model_dump()  # pragma: no cover


@pytest.mark.parametrize(
    ("endpoint", "data"),
    [
        pytest.param(_form_model_union, {"text": "1"}, id="with_form_data_of_first_member"),
        pytest.param(_form_model_union, {"other_text": "1"}, id="with_form_data_of_second_member"),
        pytest.param(_optional_form_model_union, {"text": "1"}, id="optional_with_form_data_of_first_member"),
        pytest.param(_optional_form_model_union, {"other_text": "1"}, id="optional_with_form_data_of_second_member"),
        pytest.param(
            _form_model_union_with_collections,
            {"tags": ["x", "y"], "frozen_tags": ["x", "y"], "numbers": ["1", "2"]},
            id="with_set_frozenset_and_tuple_fields",
            marks=_SKIP_IF_CADWYN_PASSES_FORM_MODEL_UNION_DUMPS_AS_THEY_ARE,
        ),
        pytest.param(_optional_form_model_union, None, id="optional_without_form_data"),
        pytest.param(_optional_form_model_union, {"unrelated": "1"}, id="optional_with_unrelated_form_data"),
    ],
)
def test__request_body_migration__with_form_model_union__head_should_get_it_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    data: Optional[dict[str, str]],
):
    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change())

    expected = TestClient(fastapi_app).post(test_path, data=data)
    for version_client in clients.values():
        response = version_client.post(test_path, data=data)
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize("data", [None, {"unrelated": "1"}], ids=["without_form_data", "with_unrelated_form_data"])
@pytest.mark.parametrize(
    ("endpoint", "migrated_body"),
    [
        pytest.param(_optional_form_model, {"v": "2"}, id="form_model"),
        pytest.param(_optional_form_model_shared_by_dependency, {"v": "2"}, id="form_model_shared_by_dependency"),
        pytest.param(_optional_form_model_with_file, {"value": "2"}, id="form_model_with_file_field"),
        pytest.param(_optional_form_model, OrderedDict({"v": "2"}), id="form_model_with_dict_subclass"),
        pytest.param(
            _optional_form_model_union,
            {"other_text": "2"},
            id="union",
            marks=_SKIP_IF_FORM_MODEL_UNIONS_ARE_NOT_FORM_MODELS,
        ),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply__head_should_get_the_body_set_by_migration(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    data: Optional[dict[str, str]],
    migrated_body: dict[str, str],
):
    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = migrated_body

    clients = create_versioned_clients(version_change(migrator=migrator))

    expected = TestClient(fastapi_app).post(test_path, data=migrated_body)
    response = clients["2000-01-01"].post(test_path, data=data)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json())
    expected = TestClient(fastapi_app).post(test_path, data=data)
    response = clients["2001-01-01"].post(test_path, data=data)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json())


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    ("endpoint", "alias"),
    [
        pytest.param(_optional_form_model, "body", id="form_model"),
        pytest.param(_optional_form_model_shared_by_dependency, "body", id="form_model_shared_by_dependency"),
        pytest.param(
            _optional_form_model_union, "text", id="union", marks=_SKIP_IF_FORM_MODEL_UNIONS_ARE_NOT_FORM_MODELS
        ),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply__head_should_get_the_body_that_migration_has_embedded_under_its_alias(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    alias: str,
):
    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)
    form_model_body = {"text": "2"} if alias == "text" else {"v": "2"}

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = {alias: form_model_body}

    clients = create_versioned_clients(version_change(migrator=migrator))

    expected = TestClient(fastapi_app).post(test_path, data=form_model_body)
    response = clients["2000-01-01"].post(test_path)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json())


class _OtherFormModelWithDefault(BaseModel):
    number: int = 5


class _OtherFormModelWithRequiredField(BaseModel):
    number: int


async def _get_other_optional_form_model_in_override(  # pragma: no cover # FastAPI >= 0.158.2
    other: Annotated[Optional[_OtherFormModelWithDefault], Form()] = None,
) -> Optional[int]:
    return None if other is None else other.number


async def _get_other_optional_form_model_with_required_field_in_override(  # pragma: no cover # FastAPI >= 0.158.2
    other: Annotated[Optional[_OtherFormModelWithRequiredField], Form()] = None,
) -> Optional[int]:
    return None if other is None else other.number


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    "override",
    [
        pytest.param(_get_fake_user, id="override_without_params"),
        pytest.param(_get_optional_form_model_value_in_override, id="override_sharing_form_model"),
        pytest.param(_get_other_optional_form_model_in_override, id="override_with_other_form_model"),
        pytest.param(
            _get_other_optional_form_model_with_required_field_in_override,
            id="override_with_other_form_model_with_required_field",
        ),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_dependency_override__head_should_get_the_body_set_by_migration(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    override: Callable[..., Coroutine[Any, Any, Any]],
):
    router.post(test_path)(_optional_form_model_next_to_overridden_dependency)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(_optional_form_model_next_to_overridden_dependency)
    fastapi_app.dependency_overrides[_get_current_user] = override

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = {"v": "2"}

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_current_user] = override

    expected = TestClient(fastapi_app).post(test_path, data={"v": "2"})
    response = clients["2000-01-01"].post(test_path)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json())


async def _optional_form_model_union_next_to_overridden_dependency(  # pragma: no cover # FastAPI >= 0.161
    text: Annotated[Optional[Union[SchemaWithText, OtherSchemaWithText]], Form()] = None,
    from_dependency: Any = Depends(_get_current_user),
):
    return [None if text is None else text.model_dump(), from_dependency]


async def _get_optional_form_model_union_in_other_order_in_override(  # pragma: no cover # FastAPI >= 0.161
    text: Annotated[Optional[Union[OtherSchemaWithText, SchemaWithText]], Form()] = None,
):
    return None if text is None else text.model_dump()


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.161
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS, reason="Before FastAPI 0.161.0, a union of form models isn't a form model"
)
def test__request_body_migration__with_optional_form_model_union_that_the_form_does_not_supply_and_override_with_union_in_other_order__head_should_get_the_body_set_by_migration(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    router.post(test_path)(_optional_form_model_union_next_to_overridden_dependency)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(_optional_form_model_union_next_to_overridden_dependency)
    override = _get_optional_form_model_union_in_other_order_in_override
    fastapi_app.dependency_overrides[_get_current_user] = override

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = {"other_text": "2"}

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_current_user] = override

    expected = TestClient(fastapi_app).post(test_path, data={"other_text": "2"})
    response = clients["2000-01-01"].post(test_path)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json())


async def _get_other_optional_form_model_under_same_alias_in_override(  # pragma: no cover # FastAPI >= 0.158.2
    body: Annotated[Optional[_OtherFormModelWithDefault], Form()] = None,
) -> Optional[int]:
    return None if body is None else body.number


# In these cases, Cadwyn can't tell where the head would look the body up, so it passes the body as the last release did
@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    ("override", "migrated_body", "expected"),
    [
        pytest.param(
            _get_other_optional_form_model_under_same_alias_in_override,
            {"v": "2"},
            [None, None],
            id="override_with_other_form_model_under_same_alias",
        ),
        pytest.param(
            _get_other_optional_form_model_in_override,
            MappingProxyType({"v": "2", "other": {"number": "3"}}),
            [None, 3],
            id="mapping_that_is_not_a_dict",
        ),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_body_that_cadwyn_cannot_embed__head_should_get_the_body_as_it_is(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    override: Callable[..., Coroutine[Any, Any, Any]],
    migrated_body: Any,
    expected: list[Any],
):
    router.post(test_path)(_optional_form_model_next_to_overridden_dependency)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = migrated_body

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_current_user] = override

    assert clients["2000-01-01"].post(test_path).json() == expected


async def _dependency_of_head_only() -> None:
    raise NotImplementedError


async def _read_whole_body_in_override(  # pragma: no cover # FastAPI >= 0.158.2
    request: Request, payload: dict[str, Any] = Body()
) -> None:
    request.state.payload = payload


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_override_of_head_only_dependency_that_reads_whole_body__override_should_get_the_body_as_it_is(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path, dependencies=[Depends(_dependency_of_head_only)])
    async def endpoint(request: Request, body: Annotated[Optional[InnerSchemaWithValidationAlias], Form()] = None):
        return [None if body is None else body.value, request.state.payload]

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = {"v": "2"}

    clients = create_versioned_clients(
        version_change(cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[]), migrator=migrator)
    )
    clients["2000-01-01"].app.dependency_overrides[_dependency_of_head_only] = _read_whole_body_in_override

    assert clients["2000-01-01"].post(test_path).json() == [None, {"v": "2"}]


async def _read_form_field_in_override(  # pragma: no cover # FastAPI >= 0.158.2
    request: Request, number: int = Form(0)
) -> None:
    request.state.number = number


class _FormModelThatForbidsExtraFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = "default"


class _FormModelThatAllowsExtraFields(BaseModel):
    model_config = ConfigDict(extra="allow")
    value: str = "default"


# The model would take the field of the override as an extra field, which FastAPI doesn't do for a form before 0.160.0
@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    "model", [_FormModelThatForbidsExtraFields, _FormModelThatAllowsExtraFields], ids=["forbids", "allows"]
)
@pytest.mark.parametrize(
    "migrated_body",
    [{"number": "3"}, {"number": "3", "value": "2"}],
    ids=["field_of_override", "fields_of_override_and_of_model"],
)
def test__request_body_migration__with_optional_form_model_with_extra_fields_that_the_form_does_not_supply_and_override_of_head_only_dependency_with_form_field__head_should_get_the_body_as_it_is(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    migrated_body: dict[str, str],
):
    @router.post(test_path, dependencies=[Depends(_dependency_of_head_only)])
    async def endpoint(request: Request, body: Annotated[Optional[model], Form()] = None):  # ty: ignore[invalid-type-form]
        return [None if body is None else body.model_dump(), request.state.number]

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = migrated_body

    clients = create_versioned_clients(
        version_change(cadwyn_endpoint(test_path, ["POST"]).had(dependencies=[]), migrator=migrator)
    )
    clients["2000-01-01"].app.dependency_overrides[_dependency_of_head_only] = _read_form_field_in_override

    assert clients["2000-01-01"].post(test_path).json() == [None, 3]


class _FormModelOfKindWithExtraFields(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["with_extra_fields"]
    label: str = "default"


class _FormModelOfOtherKind(BaseModel):
    kind: Literal["other"]


async def _optional_form_model_union_with_extra_fields(  # pragma: no cover # FastAPI >= 0.161
    body: Annotated[Optional[Union[_FormModelOfKindWithExtraFields, _FormModelOfOtherKind]], Form()] = None,
):
    return None if body is None else body.model_dump()


class _FormModelOfKindWithAliasPath(BaseModel):
    kind: Literal["with_alias_path"]
    value: str = Field("default", validation_alias=AliasPath("names", 0))


async def _optional_form_model_union_with_alias_path(  # pragma: no cover # FastAPI >= 0.161
    body: Annotated[Optional[Union[_FormModelOfKindWithAliasPath, _FormModelOfOtherKind]], Form()] = None,
):
    return None if body is None else body.model_dump()


async def _optional_form_model_union_with_root_model(  # pragma: no cover # FastAPI >= 0.161
    body: Annotated[Optional[Union[RootModel[dict[str, Any]], _FormModelOfOtherKind]], Form()] = None,
):
    return None if body is None else body.model_dump()


# FastAPI would validate the models of the union from a changed body, so Cadwyn passes the body as the last release did
@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.161
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS, reason="Before FastAPI 0.161.0, a union of form models isn't a form model"
)
@pytest.mark.parametrize(
    ("endpoint", "migrated_body"),
    [
        pytest.param(
            _optional_form_model_union_with_extra_fields,
            {"kind": "with_extra_fields", "tags": ["one"]},
            id="single_item_list_in_extra_field",
        ),
        pytest.param(
            _optional_form_model_union_with_extra_fields,
            {"kind": "with_extra_fields", "label": ""},
            id="empty_string_in_field_with_default",
        ),
        pytest.param(
            _optional_form_model_union_with_alias_path,
            {"kind": "with_alias_path", "names": ["migrated"]},
            id="single_item_list_under_alias_path",
        ),
        pytest.param(_optional_form_model_union_with_root_model, {"raw": ["migrated"]}, id="root_model"),
    ],
)
def test__request_body_migration__with_optional_form_model_union_that_the_form_does_not_supply_and_body_that_fastapi_would_change__head_should_get_the_body_as_it_is(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    migrated_body: dict[str, Any],
):
    router.post(test_path)(endpoint)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = migrated_body

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path).json() is None


class _DictWithOwnLookup(dict[str, Any]):
    @override
    def get(self, key: object, default: Any = None, /) -> Any:  # pragma: no cover # FastAPI >= 0.158.2
        return {"v": "1"} if key == "body" else super().get(key, default)


# Cadwyn can't tell what the lookups of such a dict return, so it passes the body as the last release did
@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_dict_subclass_with_own_lookup_set_by_migration__head_should_get_the_body_as_it_is(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    router.post(test_path)(_optional_form_model)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = _DictWithOwnLookup(v="2")

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path).json() == 1


class _FormModelFromString(BaseModel):
    value: str

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data: Any) -> Any:  # pragma: no cover # FastAPI >= 0.158.2
        return {"value": data} if isinstance(data, str) else data


class _FormModelFromAttributes(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    value: str


@dataclasses.dataclass
class _ObjectWithValue:
    value: str


async def _optional_form_model_from_string(  # pragma: no cover # FastAPI >= 0.158.2
    body: Annotated[Optional[_FormModelFromString], Form()] = None,
):
    return None if body is None else body.value


async def _optional_root_form_model(  # pragma: no cover # FastAPI >= 0.158.2
    body: Annotated[Optional[RootModel[str]], Form()] = None,
):
    return None if body is None else body.root


async def _optional_form_model_from_attributes(  # pragma: no cover # FastAPI >= 0.158.2
    body: Annotated[Optional[_FormModelFromAttributes], Form()] = None,
):
    return None if body is None else body.value


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    ("endpoint", "value"),
    [
        pytest.param(_optional_form_model_from_string, "2", id="string_for_model_with_before_validator"),
        pytest.param(_optional_root_form_model, "2", id="string_for_root_model"),
        pytest.param(
            _optional_form_model_from_attributes, _ObjectWithValue("2"), id="object_for_model_from_attributes"
        ),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_value_under_its_alias_set_by_migration__head_should_validate_the_value(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    value: Any,
):
    router.post(test_path)(endpoint)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = {"body": value}

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path).json() == "2"


class _FormModelWithRenamedField(BaseModel):
    kind: str
    value: int = Field(alias="x", validation_alias="v")


async def _optional_form_model_with_renamed_field(  # pragma: no cover # FastAPI >= 0.158.2
    body: Annotated[Optional[_FormModelWithRenamedField], Form()] = None,
):
    return None if body is None else body.value


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    ("endpoint", "migrated_body"),
    [
        pytest.param(_optional_form_model, {"v": "not a number"}, id="with_value_that_it_rejects"),
        # Unlike the dump of a supplied form model, the body of a migration doesn't get its validation aliases restored
        pytest.param(
            _optional_form_model_with_renamed_field,
            {"kind": "2", "x": "2"},
            id="with_serialization_alias_instead_of_validation_alias",
        ),
        pytest.param(_optional_form_model, {"body": "2"}, id="with_value_under_its_alias_that_it_rejects"),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_invalid_body_set_by_migration__should_raise_error(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    migrated_body: dict[str, str],
):
    router.post(test_path)(endpoint)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = migrated_body

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(CadwynHeadRequestValidationError):
        clients["2000-01-01"].post(test_path)


# The body may have the value of the param under its alias, so Cadwyn passes it as it is, as the last release did
@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.161
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS, reason="Before FastAPI 0.161.0, a union of form models isn't a form model"
)
def test__request_body_migration__with_optional_form_model_union_that_the_form_does_not_supply_and_body_with_field_named_like_its_alias__should_raise_error(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    router.post(test_path)(_optional_form_model_union)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = {"text": "2"}

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(CadwynHeadRequestValidationError):
        clients["2000-01-01"].post(test_path)


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_SUPPORTS_OPTIONAL_FORM_MODELS, reason="Before FastAPI 0.158.2, FastAPI doesn't omit form models"
)
@pytest.mark.parametrize(
    ("model", "migrated_body"),
    [
        pytest.param(_OtherFormModelWithDefault, {}, id="empty_for_model_with_default"),
        pytest.param(_OtherFormModelWithRequiredField, {}, id="empty_for_model_with_required_field"),
        pytest.param(_OtherFormModelWithDefault, {"unrelated": "2"}, id="unrelated_fields_for_model_with_default"),
        pytest.param(
            _OtherFormModelWithRequiredField, {"unrelated": "2"}, id="unrelated_fields_for_model_with_required_field"
        ),
        # FastAPI looks the field up by its validation alias only
        pytest.param(InnerSchemaWithValidationAlias, {"x": "2"}, id="serialization_alias_instead_of_validation_alias"),
    ],
)
def test__request_body_migration__with_optional_form_model_that_the_form_does_not_supply_and_body_without_its_fields_set_by_migration__head_should_get_none(  # noqa: E501
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    migrated_body: dict[str, str],
):
    # Like FastAPI does for a form, Cadwyn gives the param its default if the body supplies none of the model's fields
    @router.post(test_path)
    async def endpoint(body: Annotated[Optional[model], Form()] = None):  # ty: ignore[invalid-type-form]
        return None if body is None else body.model_dump()

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body = migrated_body

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path).json() is None


@pytest.mark.parametrize("field_name", ["text", "other_text"])
def test__request_body_migration__with_form_model_union__migration_should_change_the_supplied_member(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    field_name: str,
):
    router.post(test_path)(_form_model_union)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body[field_name] = "migrated"

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert {
        version: client.post(test_path, data={field_name: "sent"}).json() for version, client in clients.items()
    } == {
        "2000-01-01": {field_name: "migrated"},
        "2001-01-01": {field_name: "sent"},
    }


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.161
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS,
    reason="Before FastAPI 0.161.0, a union of form models isn't a form model",
)
@pytest.mark.parametrize(
    ("endpoint", "data", "migrated_body"),
    [
        pytest.param(_form_model_union_with_split_value, {"value": "a,b"}, {}, id="list_from_validator"),
        pytest.param(
            _form_model_union_with_split_value, {"value": "a"}, {"value": ["p", "q"]}, id="list_from_migration"
        ),
        pytest.param(_form_model_union_with_split_value, {"value": "a"}, {"value": (1, 2)}, id="tuple_from_migration"),
        pytest.param(
            _form_model_union_with_list_and_split_value,
            {"kind": "split", "value": "a,b"},
            {},
            id="list_in_member_whose_field_takes_any_value",
        ),
        pytest.param(
            _form_model_union_with_list_and_split_value,
            {"kind": "list", "value": ["a", "b"]},
            {},
            id="list_in_member_whose_field_takes_lists",
        ),
        pytest.param(
            _form_model_union_with_list_and_split_value,
            {"kind": "split", "value": "a"},
            {"kind": "list", "value": ["p", "q"]},
            id="migration_that_switches_member",
        ),
        pytest.param(
            _form_model_union_with_extra_and_tuple_value,
            {"kind": "extra", "value": "p,q"},
            {},
            id="tuple_in_extra_field_that_another_member_has_as_tuple",
        ),
    ],
)
def test__request_body_migration__with_form_model_union_and_collection_in_field_that_takes_any_value__should_fail(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    data: dict[str, str],
    migrated_body: dict[str, Any],
):
    router.post(test_path)(endpoint)

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body.update(migrated_body)

    clients = create_versioned_clients(version_change(migrator=migrator))

    # FastAPI would give only one of the values of the collection to a member whose field takes a single value,
    # so we pass the body as the last release does, which fails
    with pytest.raises(CadwynHeadRequestValidationError):
        clients["2000-01-01"].post(test_path, data=data)


class SchemaWithTextDumpedAsString(BaseModel):
    text: str

    @model_validator(mode="before")
    @classmethod
    def from_string(cls, value: Any) -> Any:
        return {"text": value} if isinstance(value, str) else value  # pragma: no cover # FastAPI >= 0.158.2

    @model_serializer
    def to_string(self) -> str:
        return self.text  # pragma: no cover # FastAPI >= 0.158.2


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so form fields can't look values up in it",
)
def test__request_body_migration__with_scalar_form_model_and_form_field_of_override_named_like_it__head_should_get_both(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(
        text: Annotated[SchemaWithTextDumpedAsString, Form()], from_dependency: str = Depends(_get_text)
    ):
        return [text.text, from_dependency]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_text] = _get_text_from_form_field

    assert {version: client.post(test_path, data={"text": "sent"}).json() for version, client in clients.items()} == {
        "2000-01-01": ["sent", "sent"],
        "2001-01-01": ["sent", "sent"],
    }


async def _get_body() -> Any:
    raise NotImplementedError


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
def test__request_body_migration__with_whole_body_param_of_override_and_clashing_body_param__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_text_from_embedded_body(text: Union[str, dict[str, str]] = Body(embed=True)) -> Any:
        return text

    async def get_body_keys(body: dict[str, Any] = Body()) -> list[str]:
        return sorted(body)

    @router.post(test_path)
    async def endpoint(
        text: Annotated[SchemaWithText, Form()],
        from_dependency: Any = Depends(_get_text),
        body_keys: Any = Depends(_get_body),
    ):
        return [text.text, from_dependency, body_keys]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides.update(
        {_get_text: get_text_from_embedded_body, _get_body: get_body_keys}
    )

    for version_client in clients.values():
        assert version_client.post(test_path, data={"text": "sent"}).json() == ["sent", {"text": "sent"}, ["text"]]


class SchemaWithOptionalText(BaseModel):
    text: Optional[str] = None
    other: str


class SchemaWithDefaultText(BaseModel):
    text: str = "model default"
    other: str


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
@pytest.mark.parametrize(
    ("form_model", "form_field_default", "expected"),
    [
        (SchemaWithOptionalText, None, [None, None]),
        (SchemaWithDefaultText, "dependency default", ["model default", "dependency default"]),
    ],
    ids=["optional", "default"],
)
def test__request_body_migration__with_form_field_of_override_named_like_unset_form_model_field__should_get_default(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    form_model: Any,
    form_field_default: Optional[str],
    expected: list[Optional[str]],
):
    async def get_text_from_form_field(text: Optional[str] = Form(form_field_default)) -> Optional[str]:
        return text

    @router.post(test_path)
    async def endpoint(text: Annotated[form_model, Form()], from_dependency: Any = Depends(_get_text)):
        return [text.text, from_dependency]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_text] = get_text_from_form_field

    for version_client in clients.values():
        assert version_client.post(test_path, data={"other": "sent"}).json() == expected


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
def test__request_body_migration__with_form_field_of_override_with_alias_choices_of_unset_field__should_get_next_one(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_value_from_form_field(value: str = Form(validation_alias=AliasChoices("text", "other"))) -> str:
        return value

    @router.post(test_path)
    async def endpoint(text: Annotated[SchemaWithOptionalText, Form()], from_dependency: Any = Depends(_get_text)):
        return [text.text, from_dependency]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_text] = get_value_from_form_field

    for version_client in clients.values():
        assert version_client.post(test_path, data={"other": "sent"}).json() == [None, "sent"]


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
def test__request_body_migration__with_required_form_field_of_override_named_like_removed_field__should_stay_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_any_from_form_field(text: Any = Form()) -> Any:
        return text

    @router.post(test_path)
    async def endpoint(text: Annotated[SchemaWithOptionalText, Form()], from_dependency: Any = Depends(_get_text)):
        return [text.text, from_dependency]

    @convert_request_to_next_version_for(SchemaWithOptionalText)
    def migrator(request: RequestInfo):
        del request.body["text"]

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_text] = get_any_from_form_field

    assert clients["2000-01-01"].post(test_path, data={"text": "sent", "other": "y"}).json() == [None, {"other": "y"}]


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
def test__request_body_migration__with_form_field_of_override_sent_outside_of_form_model__should_fail_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_text_from_form_field(text: str = Form("dependency default")) -> str:
        return text

    @router.post(test_path)
    async def endpoint(text: Annotated[OtherSchemaWithText, Form()], from_dependency: Any = Depends(_get_text)):
        return [text.other_text, from_dependency]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_text] = get_text_from_form_field

    # The form model can't carry the value of the form field, so it would silently get its default instead
    for version_client in clients.values():
        with pytest.raises(CadwynHeadRequestValidationError):
            version_client.post(test_path, data={"other_text": "x", "text": "sent"})


class SchemaWithBody(BaseModel):
    body: str
    b: str


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.161
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS,
    reason="Before FastAPI 0.161.0, a union of form models isn't a form model",
)
def test__request_body_migration__with_form_field_of_override_that_matched_union_member_lacks__should_fail_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_body_from_form_field(body: str = Form("dependency default")) -> str:
        return body

    @router.post(test_path)
    async def endpoint(
        body: Annotated[Union[OtherSchemaWithText, SchemaWithBody], Form()], from_dependency: Any = Depends(_get_body)
    ):
        return [body.model_dump(), from_dependency]

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[_get_body] = get_body_from_form_field

    # The member that the request matches can't carry the value of the form field, unlike the other member
    for version_client in clients.values():
        with pytest.raises(CadwynHeadRequestValidationError):
            version_client.post(test_path, data={"other_text": "x", "body": "sent"})


class SchemaWithOptionalBody(BaseModel):
    a: str
    body: Optional[str] = None


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
def test__request_body_migration__with_form_field_of_override_that_older_form_model_lacks__should_fail_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_body_from_form_field(body: str = Form("dependency default")) -> str:
        return body

    @router.post(test_path)
    async def endpoint(body: Annotated[SchemaWithOptionalBody, Form()], from_dependency: Any = Depends(_get_body)):
        return [body.body, from_dependency]

    clients = create_versioned_clients(version_change(schema(SchemaWithOptionalBody).field("body").didnt_exist))
    clients["2000-01-01"].app.dependency_overrides[_get_body] = get_body_from_form_field

    data = {"a": "x", "body": "sent"}
    assert clients["2001-01-01"].post(test_path, data=data).json() == ["sent", "sent"]
    # The older form model can't carry the value of the form field, so it would silently get its default instead
    with pytest.raises(CadwynHeadRequestValidationError):
        clients["2000-01-01"].post(test_path, data=data)


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.158.2
    not FASTAPI_LOOKS_UP_FORM_MODELS_BY_ALIAS,
    reason="Before FastAPI 0.158.2, Cadwyn passes the dump on as it is, so no param looks values up in it",
)
def test__request_body_migration__with_alias_choices_of_override_form_field_and_removed_field__should_get_default(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    async def get_value_from_form_field(
        value: str = Form("dependency default", validation_alias=AliasChoices("text", "external")),
    ) -> str:
        return value

    @router.post(test_path)
    async def endpoint(text: Annotated[SchemaWithOptionalText, Form()], from_dependency: Any = Depends(_get_text)):
        return [text.text, from_dependency]

    @convert_request_to_next_version_for(SchemaWithOptionalText)
    def migrator(request: RequestInfo):
        del request.body["text"]

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_text] = get_value_from_form_field

    # Like FastAPI, the form field takes the value of its first alias that the form has, which the migration removes
    data = {"text": "sent", "other": "y", "external": "ignored"}
    assert clients["2000-01-01"].post(test_path, data=data).json() == [None, "dependency default"]


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.161
    not FASTAPI_SUPPORTS_FORM_MODEL_UNIONS,
    reason="Before FastAPI 0.161.0, a union of form models isn't a form model",
)
def test__request_body_migration__with_form_model_union_and_form_field_of_override_named_like_it__head_should_get_both(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(
        text: Annotated[Union[SchemaWithText, OtherSchemaWithText], Form()], from_dependency: str = Depends(_get_text)
    ):
        return [text.model_dump(), from_dependency]

    @convert_request_to_next_version_for(test_path, ["POST"])
    def migrator(request: RequestInfo):
        request.body["text"] = "migrated"

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[_get_text] = _get_text_from_form_field

    assert {version: client.post(test_path, data={"text": "sent"}).json() for version, client in clients.items()} == {
        "2000-01-01": [{"text": "migrated"}, "migrated"],
        "2001-01-01": [{"text": "sent"}, "sent"],
    }


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


class SchemaWithJsonDict(BaseModel):
    data: Json[dict[str, Any]]


class SchemaWithNestedJsonDicts(BaseModel):
    inner: SchemaWithJsonDict
    items: list[SchemaWithJsonDict]
    by_key: dict[str, SchemaWithJsonDict] = {}
    plain: Optional["InnerOfJson"] = None
    pair: tuple[int, int] = (0, 0)
    tag: Union[int, str] = 0


class SchemaWithOptionalJsonDict(BaseModel):
    data: Optional[Json[dict[str, Any]]] = None


class SchemaWithJsonDatetimes(BaseModel):
    data: Json[dict[str, datetime]]


class OtherSchemaWithJsonList(BaseModel):
    data: Json[list[int]]


class InnerOfJson(BaseModel):
    x: int = 0
    tags: list[str] = []
    labels: list[str] = Field(default_factory=list)


class InnerOfJsonWithComputedField(BaseModel):
    x: int = 0

    @computed_field
    @property
    def double(self) -> int:
        return self.x * 2


class _PlainJsonEnum(Enum):
    LOW = "low"
    HIGH = "high"


class SchemaWithJsonModels(BaseModel):
    one: Json[InnerOfJson]
    many: Json[list[InnerOfJson]]
    by_key: Json[dict[str, InnerOfJson]] = {}
    maybe: Json[Optional[InnerOfJson]] = None
    root: Json[RootModel[list[int]]] = RootModel[list[int]]([])
    values: Json[dict[str, Union[int, str]]] = {}
    level: Json[_PlainJsonEnum] = _PlainJsonEnum.LOW
    choice: Json[Literal[1, 2]] = 1
    mixed: Json[dict[str, Union[int, Any]]] = {}
    anything: Json[Any] = None
    untyped: Json[dict] = {}


class SchemaWithJsonByAlias(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    data: Json[dict[str, Any]] = Field(alias="d")


class JsonTree(BaseModel):
    data: Json[dict[str, Any]]
    children: list["JsonTree"] = []


class IntTree(BaseModel):
    x: int = 0
    children: list["IntTree"] = []


class SchemaWithJsonDictAndTree(BaseModel):
    data: Json[dict[str, Any]]
    tree: Optional["IntTree"] = None


class JsonRootModel(RootModel[list[SchemaWithJsonDict]]):
    pass


class InnerOfJsonWithEmptyAlias(BaseModel):
    x: int = Field(default=0, alias="")


class SchemaWithJsonDictAndComplexNumber(BaseModel):
    data: Json[dict[str, Any]]
    number: complex = 0j


class SchemaWithOptionalJsonDictAndTupleKeys(BaseModel):
    data: Optional[Json[dict[str, Any]]] = None
    other: dict[tuple[int, int], int] = {(1, 2): 3}
    extra: Any = frozenset({1})


class SchemaWithSerializedJsonDict(BaseModel):
    data: Annotated[Json[dict[str, Any]], PlainSerializer(lambda value: json.dumps(value))]


async def _json_form_model_endpoint(body: Annotated[SchemaWithJsonDict, Form()]):
    return body


async def _json_body_endpoint(body: SchemaWithJsonDict):
    return body


async def _nested_json_body_endpoint(body: SchemaWithNestedJsonDicts):
    return body


async def _optional_json_body_endpoint(body: SchemaWithOptionalJsonDict):
    return body


async def _json_datetimes_body_endpoint(body: SchemaWithJsonDatetimes):
    return body


async def _union_of_json_bodies_endpoint(body: Union[OtherSchemaWithJsonList, SchemaWithJsonDict]):
    raise NotImplementedError


async def _raw_json_body_endpoint(body: Annotated[Json[dict[str, Any]], Body()]):
    return body


async def _json_models_body_endpoint(body: SchemaWithJsonModels):
    return body


async def _json_by_alias_body_endpoint(body: SchemaWithJsonByAlias):
    return body.data


def _read_json_body_model(body: SchemaWithJsonDict) -> dict[str, Any]:
    return body.data


async def _json_body_with_dependency_of_the_same_model_endpoint(
    body: SchemaWithJsonDict, from_dependency: Annotated[dict[str, Any], Depends(_read_json_body_model)]
):
    return {"endpoint": body.data, "dependency": from_dependency}


async def _json_tree_body_endpoint(body: JsonTree):
    raise NotImplementedError


async def _json_with_tree_endpoint(body: SchemaWithJsonDictAndTree):
    return body


async def _json_root_model_body_endpoint(body: JsonRootModel):
    return body


async def _json_with_complex_number_endpoint(body: SchemaWithJsonDictAndComplexNumber):
    raise NotImplementedError


async def _optional_json_and_tuple_keys_endpoint(body: SchemaWithOptionalJsonDictAndTupleKeys):
    return {"data": body.data, "other": sorted(body.other.values())}


async def _serialized_json_body_endpoint(body: SchemaWithSerializedJsonDict):
    return body.data


async def _json_model_with_empty_alias_endpoint(body: Annotated[Json[InnerOfJsonWithEmptyAlias], Body()]):
    return body


@pytest.mark.parametrize(
    ("endpoint", "request_kwargs"),
    [
        pytest.param(_json_form_model_endpoint, {"data": {"data": '{"a": 1}'}}, id="form_model"),
        pytest.param(_json_body_endpoint, {"json": {"data": '{"a": 1}'}}, id="json_body"),
        # A dependency that reads the body into the same model validates it as the endpoint does
        pytest.param(
            _json_body_with_dependency_of_the_same_model_endpoint,
            {"json": {"data": '{"a": 1}'}},
            id="dependency_with_the_same_body_model",
        ),
        pytest.param(
            _nested_json_body_endpoint,
            {
                "json": {
                    "inner": {"data": '{"a": 1}'},
                    "items": [{"data": '{"b": 2}'}, {"data": '{"c": 3}'}],
                    "by_key": {"k": {"data": '{"d": 4}'}},
                    "tag": "x",
                }
            },
            id="nested_models",
        ),
        pytest.param(_optional_json_body_endpoint, {"json": {"data": '{"a": 1}'}}, id="optional_with_value"),
        pytest.param(_optional_json_body_endpoint, {"json": {"data": None}}, id="optional_with_none"),
        pytest.param(
            _json_datetimes_body_endpoint, {"json": {"data": '{"at": "2000-01-01T00:00:00"}'}}, id="datetimes"
        ),
        pytest.param(_raw_json_body_endpoint, {"json": '{"a": 1}'}, id="raw_body"),
        pytest.param(
            _json_models_body_endpoint,
            {
                "json": {
                    "one": '{"x": 1, "tags": ["a"]}',
                    "many": '[{"x": 2}, {}]',
                    "by_key": '{"k": {"x": 3}}',
                    "maybe": '{"x": 9}',
                    "root": "[5]",
                    "values": '{"a": 6, "b": "c"}',
                    "level": '"high"',
                    "choice": "2",
                    "mixed": '{"a": 1, "b": [2]}',
                    "anything": '{"a": [1, 2.5, true, null, "s"]}',
                    "untyped": '{"a": {"b": 1}}',
                }
            },
            id="models_in_json",
        ),
        # The plan has the fields that the request leaves out too
        pytest.param(
            _json_models_body_endpoint, {"json": {"one": '{"x": 1}', "many": "[]"}}, id="models_in_json_with_defaults"
        ),
        pytest.param(_json_root_model_body_endpoint, {"json": [{"data": '{"a": 1}'}]}, id="root_model"),
        pytest.param(
            _json_with_tree_endpoint,
            {"json": {"data": '{"a": 1}', "tree": {"x": 1, "children": [{"x": 2}]}}},
            id="self_referencing_model_without_json",
        ),
        # Serializers that dump the value as JSON are a workaround that works as before
        pytest.param(_serialized_json_body_endpoint, {"json": {"data": '{"a": 1}'}}, id="value_dumped_as_json"),
        pytest.param(_json_model_with_empty_alias_endpoint, {"json": '{"": 5}'}, id="empty_alias_in_json"),
        pytest.param(_optional_json_and_tuple_keys_endpoint, {"json": {"other": {}}}, id="default_with_tuple_keys"),
        pytest.param(_json_by_alias_body_endpoint, {"json": {"d": '{"a": 1}'}}, id="alias"),
        pytest.param(_json_by_alias_body_endpoint, {"json": {"data": '{"a": 1}'}}, id="name"),
    ],
)
def test__request_body_migration__with_json_fields__should_be_the_same_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    request_kwargs: dict[str, Any],
):
    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    expected = TestClient(fastapi_app).post(test_path, **request_kwargs)
    assert expected.status_code == 200
    for version_client in clients.values():
        response = version_client.post(test_path, **request_kwargs)
        assert (response.status_code, response.json()) == (expected.status_code, expected.json())


def test__request_body_migration__with_json_field__should_solve_the_head_once(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    solvers: list[str] = []

    async def dependency(solver: str = Depends(current_dependency_solver)) -> None:
        solvers.append(solver)

    @router.post(test_path, dependencies=[Depends(dependency, use_cache=False)])
    async def endpoint(body: SchemaWithJsonDict):
        return body.data

    clients = create_versioned_clients(version_change())

    assert clients["2000-01-01"].post(test_path, json={"data": '{"a": 1}'}).json() == {"a": 1}
    assert solvers == ["fastapi", "cadwyn"]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # A migration that encodes the value itself keeps working as before
        pytest.param('{"a": 1}', {"encoded_by_migration": True}, id="value_that_the_migration_encodes"),
        pytest.param('{"b": 2}', {"set_by_migration": True}, id="value_that_the_migration_sets"),
        pytest.param('{"c": 3}', {"c": 3}, id="value_that_the_migration_keeps"),
    ],
)
def test__request_body_migration__with_json_field__should_get_its_parsed_value_and_keep_the_value_it_sets(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    data: str,
    expected: dict[str, Any],
):
    seen_bodies = []

    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonDict):
        return body.data

    @convert_request_to_next_version_for(SchemaWithJsonDict)
    def migrator(request: RequestInfo):
        seen_bodies.append(dict(request.body))
        if request.body["data"] == {"a": 1}:
            request.body["data"] = '{"encoded_by_migration": true}'
        elif request.body["data"] == {"b": 2}:
            request.body["data"] = {"set_by_migration": True}

    clients = create_versioned_clients(version_change(migrator=migrator))

    assert clients["2000-01-01"].post(test_path, json={"data": data}).json() == expected
    assert seen_bodies == [{"data": json.loads(data)}]


class _NotJsonSerializable:
    pass


@pytest.mark.parametrize(
    ("value", "expected_error_type"),
    [
        pytest.param(["not", "a", "dict"], "json_type", id="value_of_another_type"),
        # Encoded, the keys would collide, and the decimal would turn into a string
        pytest.param({1: "integer", "1": "text"}, "json_type", id="key_of_another_type"),
        pytest.param({"a": Decimal("1.2300")}, "json_type", id="value_that_isnt_json_in_any"),
        pytest.param({"a": {1: "integer"}}, "json_type", id="key_that_isnt_a_string_in_any"),
        pytest.param(_NotJsonSerializable(), "json_type", id="value_that_cant_be_encoded"),
        pytest.param("not json", "json_invalid", id="string"),
    ],
)
def test__request_body_migration__with_json_field_that_the_head_rejects__should_raise_head_validation_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    value: Any,
    expected_error_type: str,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonDict):
        raise NotImplementedError

    @convert_request_to_next_version_for(SchemaWithJsonDict)
    def migrator(request: RequestInfo):
        request.body["data"] = value

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": '{"a": 1}'})
    assert [(error["type"], error["loc"]) for error in exc_info.value.errors] == [
        (expected_error_type, ("body", "data"))
    ]


class SchemaWithJsonDictCopiedBeforeValidation(BaseModel):
    data: Annotated[
        Json[dict[str, Any]], BeforeValidator(lambda value: dict(value) if isinstance(value, dict) else value)
    ]


class SchemaWithBase64JsonBytes(BaseModel):
    model_config = ConfigDict(val_json_bytes="base64", ser_json_bytes="base64")

    data: Json[dict[str, bytes]]


class SchemaWithJsonDictOrInt(BaseModel):
    data: Union[Json[dict[str, Any]], int]


class SchemaWithJsonDictOrSchemaWithJsonDict(BaseModel):
    data: Union[Json[dict[str, Any]], SchemaWithJsonDict]


class InnerOfJsonWithSerializationAlias(BaseModel):
    x: int = Field(default=0, serialization_alias="x_out")


class InnerOfJsonWithExcludedField(BaseModel):
    x: int = Field(default=0, exclude=True)


class SchemaWithJsonModelWithSerializationAlias(BaseModel):
    data: Json[InnerOfJsonWithSerializationAlias]
    also: Optional[Json[InnerOfJsonWithSerializationAlias]] = None


class SchemaWithJsonModelWithExcludedField(BaseModel):
    data: Json[InnerOfJsonWithExcludedField]


class InnerOfJsonWithFieldSerializer(BaseModel):
    x: int = 0

    @field_serializer("x")
    def serialize_x(self, value: int) -> int:
        return value * 10


class InnerOfJsonWithBeforeValidator(BaseModel):
    x: int = 0

    @field_validator("x", mode="before")
    @classmethod
    def validate_x(cls, value: Any) -> Any:
        return value


class InnerOfJsonWithFieldsDumpedToTheSameKey(BaseModel):
    a: int = Field(default=0, alias="s")
    b: int = Field(default=0, alias="s")


class InnerOfJsonWithComputedFieldForbiddingExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = 0

    @computed_field
    @property
    def double(self) -> int:
        return self.x * 2


class SchemaWithJsonDictShadowedByComputedField(BaseModel):
    data: Json[dict[str, Any]] = Field(validation_alias="raw", serialization_alias="raw_out")

    @computed_field(alias="raw")
    @property
    def computed(self) -> dict[str, Any]:
        return {"computed": True}


class InnerOfJsonWithDoublingFieldValidator(BaseModel):
    x: int = 0

    @field_validator("x")
    @classmethod
    def double_x(cls, value: int) -> int:
        return value * 2


class InnerOfJsonWithDoublingModelValidator(BaseModel):
    x: int = 0

    @model_validator(mode="after")
    def double_x(self) -> Self:
        self.x *= 2
        return self


class InnerOfJsonWithDoublingPostInit(BaseModel):
    x: int = 0

    @override
    def model_post_init(self, context: Any, /) -> None:
        self.x *= 2


class SchemaWithJsonDictBehindDoublingValidator(BaseModel):
    data: Annotated[Json[dict[str, int]], AfterValidator(lambda value: {key: item * 2 for key, item in value.items()})]


class SchemaWithJsonDictUnderEmptyAlias(BaseModel):
    data: Json[dict[str, Any]] = Field(validation_alias="")
    other: dict[str, Any] = Field(default_factory=dict, serialization_alias="")


class SchemaWithJsonDictAllowingExtra(BaseModel):
    model_config = ConfigDict(extra="allow")

    data: Json[dict[str, Any]] = Field(validation_alias=AliasChoices("raw", "out"), serialization_alias="out")


class SchemaWithJsonDictAndAliasedSibling(BaseModel):
    data: Json[dict[str, Any]]
    x: int = Field(default=0, serialization_alias="out")


class SchemaWithOptionalJsonDictWithoutDefault(BaseModel):
    data: Json[Optional[dict[str, Any]]]


_json_default_counter = itertools.count(1)


class InnerOfJsonWithCountingDefaultFactory(BaseModel):
    x: int = Field(default_factory=lambda: next(_json_default_counter))


class InnerOfJsonWithStrOrDecimal(BaseModel):
    amount: Union[str, Decimal]


class SchemaWithJsonDictAndValidatedSibling(BaseModel):
    data: Json[dict[str, Any]]
    name: Annotated[str, AfterValidator(lambda value: value + "!")]


class SchemaWithJsonDictAndPathSibling(BaseModel):
    data: Json[dict[str, Any]]
    path: pathlib.Path


class SchemaWithJsonDictAndCountingSibling(BaseModel):
    data: Json[dict[str, Any]]
    number: int = Field(default_factory=lambda: next(_json_default_counter))


class SchemaWithJsonDictAndJsonStrOrDecimal(BaseModel):
    data: Json[dict[str, Any]]
    value: Json[Union[str, Decimal]]


class SchemaWithJsonDictAndJsonInUnion(BaseModel):
    data: Json[dict[str, Any]]
    value: Union[Json[Union[str, int]], int]


class SchemaWithJsonDictAndJsonAny(BaseModel):
    data: Json[dict[str, Any]]
    value: Json[Any]


class JsonNode(BaseModel):
    value: Json[Union[str, int]]
    children: list["JsonNode"] = []


_UpperString = Annotated[str, StringConstraints(pattern="^a", to_upper=True)]
_LowerString = Annotated[str, StringConstraints(to_lower=True)]


class SchemaWithJsonUnionOfConstrainedStrings(BaseModel):
    data: Json[list[Union[_UpperString, _LowerString]]]


class SchemaWithJsonDictAndUnionOfConstrainedStrings(BaseModel):
    data: Json[dict[str, Any]]
    tag: Union[_UpperString, _LowerString]


class _MemberWithA(BaseModel):
    a: int = 0
    b: str = "A"


class _MemberWithAliasA(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    x: int = Field(default=0, alias="a")
    b: str = "B"


class SchemaWithJsonDictAndUnionOfModels(BaseModel):
    data: Json[dict[str, Any]]
    item: Union[_MemberWithA, _MemberWithAliasA]


class SchemaWithJsonDictAndUnionInUpperCaseModel(BaseModel):
    model_config = ConfigDict(str_to_upper=True)

    data: Json[dict[str, Any]]
    tag: Union[str, int]


class SchemaWithJsonDictAndIterableOfUnion(BaseModel):
    data: Json[dict[str, Any]]
    values: Iterable[Union[_UpperString, _LowerString]]


class SchemaWithJsonDictAndIterable(BaseModel):
    data: Json[dict[str, Any]]
    values: Iterable[int]


class SchemaWithJsonKeys(BaseModel):
    keys: dict[Json[Any], int]
    data: Json[dict[str, Any]]


class _EnumWithCollidingValues(Enum):
    NUMBER = Decimal(1)
    TEXT = "1"


class SchemaWithJsonEnumWithCollidingValues(BaseModel):
    data: Json[_EnumWithCollidingValues]


class SchemaWithJsonFrozenset(BaseModel):
    data: Json[frozenset[str]]


class SchemaWithJsonDictAndSet(BaseModel):
    data: Json[dict[str, Any]]
    tags: set[str]


class _ReusedModelWithJson(BaseModel):
    value: Json[Any]


class SchemaReferringToItselfWithReusedJsonModel(BaseModel):
    first: Optional[_ReusedModelWithJson] = None
    second: Optional[_ReusedModelWithJson] = None
    children: list["SchemaReferringToItselfWithReusedJsonModel"] = []


class SchemaWithJsonLiteralOfDecimalOrString(BaseModel):
    data: Json[Literal[Decimal("1.5"), "1.5"]]  # ty: ignore[invalid-type-form]


_computed_field_counter = itertools.count(1)


class SchemaWithJsonDictAndCountingComputedField(BaseModel):
    data: Json[dict[str, Any]]

    @computed_field
    @property
    def number(self) -> int:
        return next(_computed_field_counter)


class SchemaWithJsonModelWithComputedField(BaseModel):
    data: Json[InnerOfJsonWithComputedField]


class SchemaWithJsonFloat(BaseModel):
    data: Json[float]


class SchemaWithJsonDictOfFloatKeys(BaseModel):
    data: Json[dict[float, int]]


class SchemaWithJsonDictBehindModelValidator(BaseModel):
    data: Json[dict[str, Any]]

    @model_validator(mode="before")
    @classmethod
    def validate_model(cls, value: Any) -> Any:
        return value


async def _json_dict_copied_before_validation_endpoint(body: SchemaWithJsonDictCopiedBeforeValidation):
    raise NotImplementedError


async def _base64_json_bytes_endpoint(body: SchemaWithBase64JsonBytes):
    raise NotImplementedError


async def _json_or_int_body_endpoint(body: SchemaWithJsonDictOrInt):
    raise NotImplementedError


async def _json_or_schema_with_json_body_endpoint(body: SchemaWithJsonDictOrSchemaWithJsonDict):
    raise NotImplementedError


async def _json_model_with_serialization_alias_endpoint(body: SchemaWithJsonModelWithSerializationAlias):
    raise NotImplementedError


async def _json_model_with_excluded_field_endpoint(body: SchemaWithJsonModelWithExcludedField):
    raise NotImplementedError


async def _json_model_with_field_serializer_endpoint(body: Annotated[Json[InnerOfJsonWithFieldSerializer], Body()]):
    raise NotImplementedError


async def _json_model_with_before_validator_endpoint(body: Annotated[Json[InnerOfJsonWithBeforeValidator], Body()]):
    raise NotImplementedError


async def _json_model_with_fields_dumped_to_the_same_key_endpoint(
    body: Annotated[Json[InnerOfJsonWithFieldsDumpedToTheSameKey], Body()],
):
    raise NotImplementedError


async def _json_union_with_model_endpoint(body: Annotated[Json[Union[InnerOfJson, int]], Body()]):
    raise NotImplementedError


async def _json_model_with_computed_field_forbidding_extra_endpoint(
    body: Annotated[Json[InnerOfJsonWithComputedFieldForbiddingExtra], Body()],
):
    raise NotImplementedError


async def _json_shadowed_by_computed_field_endpoint(body: SchemaWithJsonDictShadowedByComputedField):
    raise NotImplementedError


async def _json_model_with_doubling_field_validator_endpoint(
    body: Annotated[Json[InnerOfJsonWithDoublingFieldValidator], Body()],
):
    raise NotImplementedError


async def _json_model_with_doubling_model_validator_endpoint(
    body: Annotated[Json[InnerOfJsonWithDoublingModelValidator], Body()],
):
    raise NotImplementedError


async def _json_model_with_doubling_post_init_endpoint(body: Annotated[Json[InnerOfJsonWithDoublingPostInit], Body()]):
    raise NotImplementedError


async def _json_list_with_doubling_item_validator_endpoint(
    body: Annotated[Json[list[Annotated[int, AfterValidator(lambda value: value * 2)]]], Body()],
):
    raise NotImplementedError


async def _json_behind_doubling_validator_endpoint(body: SchemaWithJsonDictBehindDoublingValidator):
    raise NotImplementedError


async def _json_under_empty_alias_endpoint(body: SchemaWithJsonDictUnderEmptyAlias):
    raise NotImplementedError


async def _json_allowing_extra_endpoint(body: SchemaWithJsonDictAllowingExtra):
    raise NotImplementedError


async def _json_with_aliased_sibling_endpoint(body: SchemaWithJsonDictAndAliasedSibling):
    raise NotImplementedError


async def _optional_json_without_default_endpoint(body: SchemaWithOptionalJsonDictWithoutDefault):
    raise NotImplementedError


async def _json_model_with_counting_default_factory_endpoint(
    body: Annotated[Json[InnerOfJsonWithCountingDefaultFactory], Body()],
):
    raise NotImplementedError


async def _json_model_with_str_or_decimal_endpoint(body: Annotated[Json[InnerOfJsonWithStrOrDecimal], Body()]):
    raise NotImplementedError


async def _json_list_of_str_or_timedelta_endpoint(body: Annotated[Json[list[Union[str, timedelta]]], Body()]):
    raise NotImplementedError


async def _json_str_or_datetime_endpoint(body: Annotated[Json[Union[str, datetime]], Body()]):
    raise NotImplementedError


async def _json_literal_or_decimal_endpoint(body: Annotated[Json[Union[Literal["1.5"], Decimal]], Body()]):
    raise NotImplementedError


async def _json_with_validated_sibling_endpoint(body: SchemaWithJsonDictAndValidatedSibling):
    raise NotImplementedError


async def _json_with_path_sibling_endpoint(body: SchemaWithJsonDictAndPathSibling):
    raise NotImplementedError


async def _json_with_counting_sibling_endpoint(body: SchemaWithJsonDictAndCountingSibling):
    raise NotImplementedError


async def _json_in_json_endpoint(body: Annotated[Json[list[Json[int]]], Body()]):
    raise NotImplementedError


async def _json_tuple_with_model_endpoint(body: Annotated[Json[tuple[InnerOfJson, int]], Body()]):
    raise NotImplementedError


async def _json_and_json_str_or_decimal_endpoint(body: SchemaWithJsonDictAndJsonStrOrDecimal):
    raise NotImplementedError


async def _json_and_json_in_union_endpoint(body: SchemaWithJsonDictAndJsonInUnion):
    raise NotImplementedError


async def _json_and_json_any_endpoint(body: SchemaWithJsonDictAndJsonAny):
    raise NotImplementedError


async def _json_node_endpoint(body: JsonNode):
    raise NotImplementedError


async def _json_union_of_constrained_strings_endpoint(body: SchemaWithJsonUnionOfConstrainedStrings):
    raise NotImplementedError


async def _json_and_union_of_constrained_strings_endpoint(body: SchemaWithJsonDictAndUnionOfConstrainedStrings):
    raise NotImplementedError


async def _json_and_union_of_models_endpoint(body: SchemaWithJsonDictAndUnionOfModels):
    raise NotImplementedError


async def _json_and_union_in_upper_case_model_endpoint(body: SchemaWithJsonDictAndUnionInUpperCaseModel):
    raise NotImplementedError


async def _json_and_iterable_of_union_endpoint(body: SchemaWithJsonDictAndIterableOfUnion):
    raise NotImplementedError


async def _json_and_iterable_endpoint(body: SchemaWithJsonDictAndIterable):
    raise NotImplementedError


async def _json_dict_with_optional_keys_endpoint(body: Annotated[Json[dict[Optional[str], int]], Body()]):
    raise NotImplementedError


async def _json_union_with_list_endpoint(body: Annotated[Json[Union[int, list[int]]], Body()]):
    raise NotImplementedError


async def _json_keys_endpoint(body: SchemaWithJsonKeys):
    raise NotImplementedError


async def _json_enum_with_colliding_values_endpoint(body: SchemaWithJsonEnumWithCollidingValues):
    raise NotImplementedError


async def _json_frozenset_endpoint(body: SchemaWithJsonFrozenset):
    raise NotImplementedError


async def _json_and_set_endpoint(body: SchemaWithJsonDictAndSet):
    raise NotImplementedError


async def _self_reference_with_reused_json_model_endpoint(body: SchemaReferringToItselfWithReusedJsonModel):
    raise NotImplementedError


async def _json_literal_of_decimal_or_string_endpoint(body: SchemaWithJsonLiteralOfDecimalOrString):
    raise NotImplementedError


async def _json_and_counting_computed_field_endpoint(body: SchemaWithJsonDictAndCountingComputedField):
    raise NotImplementedError


async def _json_model_with_computed_field_endpoint(body: SchemaWithJsonModelWithComputedField):
    raise NotImplementedError


async def _json_float_endpoint(body: SchemaWithJsonFloat):
    raise NotImplementedError


async def _json_dict_of_float_keys_endpoint(body: SchemaWithJsonDictOfFloatKeys):
    raise NotImplementedError


async def _json_behind_model_validator_endpoint(body: SchemaWithJsonDictBehindModelValidator):
    raise NotImplementedError


async def _int_tree_in_json_endpoint(body: Annotated[Json[IntTree], Body()]):
    raise NotImplementedError


async def _json_tree_with_children_endpoint(body: JsonTree):
    raise NotImplementedError


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        pytest.param(
            _json_dict_copied_before_validation_endpoint,
            {"data": '{"a": 1}'},
            id="value_that_a_validator_gets_before_json",
        ),
        pytest.param(_base64_json_bytes_endpoint, {"data": '{"k": "dGVzdA=="}'}, id="bytes"),
        pytest.param(_json_or_int_body_endpoint, {"data": '{"a": 1}'}, id="union_with_a_member_without_json"),
        pytest.param(
            _json_or_schema_with_json_body_endpoint, {"data": '{"data": {"a": 1}}'}, id="json_member_of_union"
        ),
        pytest.param(
            _json_or_schema_with_json_body_endpoint, {"data": {"data": '{"a": 1}'}}, id="model_member_of_union"
        ),
        # The head would give the fields their defaults instead of the values that the client has sent
        pytest.param(_json_model_with_serialization_alias_endpoint, {"data": '{"x": 5}'}, id="serialization_alias"),
        pytest.param(_json_model_with_excluded_field_endpoint, {"data": '{"x": 5}'}, id="excluded_field"),
        pytest.param(_json_model_with_field_serializer_endpoint, '{"x": 5}', id="field_serializer"),
        pytest.param(_json_model_with_before_validator_endpoint, '{"x": 5}', id="before_validator_in_json"),
        pytest.param(_json_model_with_fields_dumped_to_the_same_key_endpoint, '{"s": 5}', id="same_dumped_key"),
        pytest.param(_json_union_with_model_endpoint, '{"x": 5}', id="union_with_model_in_json"),
        pytest.param(_json_behind_model_validator_endpoint, {"data": '{"a": 1}'}, id="model_validator"),
        # The head would decode the strings of the other `Json[...]` values once more
        pytest.param(
            _json_and_json_str_or_decimal_endpoint, {"data": "{}", "value": '"5"'}, id="string_in_json_we_cant_encode"
        ),
        pytest.param(_json_and_json_in_union_endpoint, {"data": "{}", "value": '"5"'}, id="string_in_json_in_union"),
        pytest.param(_json_and_json_any_endpoint, {"data": "{}", "value": '"123"'}, id="string_in_json_we_encode"),
        pytest.param(
            _json_node_endpoint, {"value": "1", "children": [{"value": '"5"'}]}, id="string_in_json_of_self_reference"
        ),
        # The head would pick another member for the dump than the request's version has picked
        pytest.param(_union_of_json_bodies_endpoint, {"data": '{"a": 1}'}, id="union_of_models_with_json"),
        pytest.param(_json_tree_body_endpoint, {"data": '{"a": 1}'}, id="self_referencing_model"),
        pytest.param(
            _json_union_of_constrained_strings_endpoint, {"data": '["abc"]'}, id="union_of_constrained_strings_in_json"
        ),
        pytest.param(
            _json_and_union_of_constrained_strings_endpoint,
            {"data": "{}", "tag": "abc"},
            id="union_of_constrained_strings",
        ),
        pytest.param(_json_and_union_of_models_endpoint, {"data": "{}", "item": {"x": 7}}, id="union_of_models"),
        # We don't walk into the types of pydantic that we don't know, e.g. their unions
        pytest.param(
            _json_and_iterable_of_union_endpoint, {"data": "{}", "values": ["abc"]}, id="union_in_unknown_type"
        ),
        pytest.param(_json_and_iterable_endpoint, {"data": "{}", "values": [1]}, id="unknown_type"),
        # Encoding changes these values, so the head would read other values than the client has sent
        pytest.param(_json_keys_endpoint, {"keys": {'"123"': 1}, "data": "{}"}, id="json_in_dict_keys"),
        pytest.param(_json_dict_with_optional_keys_endpoint, '{"a": 1}', id="keys_we_cant_check_in_json"),
        pytest.param(_json_union_with_list_endpoint, "[1]", id="union_with_list_in_json"),
        # The head builds sets anew from the order of the dumped sets, so their order might differ from FastAPI's
        pytest.param(_json_frozenset_endpoint, {"data": '["a", "f", "h"]'}, id="set_in_json"),
        # JSON loses the sign of NaN
        pytest.param(_json_float_endpoint, {"data": '"-nan"'}, id="negative_nan"),
        pytest.param(_json_dict_of_float_keys_endpoint, {"data": '{"-nan": 1}'}, id="negative_nan_key"),
        # The model with the `Json[...]` value is a definition of its own below the model that refers to itself
        pytest.param(
            _self_reference_with_reused_json_model_endpoint,
            {"first": {"value": '{"x": 1}'}, "children": [{"first": {"value": '"[1]"'}}]},
            id="self_referencing_model_with_reused_json_model",
        ),
        # Encoded, the decimal would be a string, which is the other value
        pytest.param(_json_literal_of_decimal_or_string_endpoint, {"data": "1.5"}, id="literal_of_decimal_or_string"),
        # The dump computes the computed fields once more
        pytest.param(_json_and_counting_computed_field_endpoint, {"data": "{}"}, id="computed_field"),
        pytest.param(_json_model_with_computed_field_endpoint, {"data": '{"x": 4}'}, id="computed_field_in_json"),
        pytest.param(_json_and_set_endpoint, {"data": "{}", "tags": ["a", "f", "h"]}, id="set"),
        pytest.param(_json_enum_with_colliding_values_endpoint, {"data": "1"}, id="enum_with_colliding_values"),
        pytest.param(_json_with_complex_number_endpoint, {"data": '{"a": 1}', "number": "-0-0j"}, id="complex_number"),
        pytest.param(
            _json_and_union_in_upper_case_model_endpoint, {"data": "{}", "tag": "abc"}, id="union_in_upper_case_model"
        ),
        # The head would apply the validator of the sibling once more
        pytest.param(_json_with_validated_sibling_endpoint, {"data": "{}", "name": "a"}, id="validator_of_sibling"),
        pytest.param(
            _json_with_path_sibling_endpoint, {"data": "{}", "path": "/a"}, id="pydantic_validator_of_sibling"
        ),
        pytest.param(_json_with_counting_sibling_endpoint, {"data": "{}"}, id="default_factory_of_sibling"),
        pytest.param(_json_in_json_endpoint, '["1"]', id="json_in_json"),
        pytest.param(_json_tuple_with_model_endpoint, '[{"x": 1}, 2]', id="model_in_tuple_in_json"),
        # The dumped extra key overwrites the dumped field
        pytest.param(
            _json_allowing_extra_endpoint, {"raw": '{"client": 1}', "out": {"other": 2}}, id="model_allowing_extra"
        ),
        # The head would give the sibling its default instead of the client's value
        pytest.param(_json_with_aliased_sibling_endpoint, {"data": "{}", "x": 5}, id="sibling_dumped_to_another_key"),
        pytest.param(_optional_json_without_default_endpoint, {"data": "null"}, id="null"),
        # The head would call the default factory once more
        pytest.param(_json_model_with_counting_default_factory_endpoint, "{}", id="default_factory_in_json"),
        # The encoded JSON has strings where the client has sent numbers, so the head would pick another member
        pytest.param(_json_model_with_str_or_decimal_endpoint, '{"amount": 9.99}', id="union_with_decimal_in_json"),
        pytest.param(_json_list_of_str_or_timedelta_endpoint, "[86400]", id="union_with_timedelta_in_json"),
        pytest.param(_json_str_or_datetime_endpoint, "1700000000", id="union_with_datetime_in_json"),
        pytest.param(_json_literal_or_decimal_endpoint, "1.5", id="union_with_literal_and_decimal_in_json"),
        # The dump of the request's version has the validated values, which the head would transform once more
        pytest.param(_json_model_with_doubling_field_validator_endpoint, '{"x": 3}', id="field_validator_in_json"),
        pytest.param(_json_model_with_doubling_model_validator_endpoint, '{"x": 3}', id="model_validator_in_json"),
        pytest.param(_json_model_with_doubling_post_init_endpoint, '{"x": 3}', id="post_init_in_json"),
        pytest.param(_json_list_with_doubling_item_validator_endpoint, "[3]", id="item_validator_in_json"),
        pytest.param(_json_behind_doubling_validator_endpoint, {"data": '{"a": 3}'}, id="after_validator_of_json"),
        # The head would read the dumped value of `other` for `data`, because an empty alias is still an alias
        pytest.param(_json_under_empty_alias_endpoint, {"": '{"client": 1}', "other": {"shadow": 2}}, id="empty_alias"),
        pytest.param(
            _json_model_with_computed_field_forbidding_extra_endpoint,
            '{"x": 5}',
            id="computed_field_of_model_forbidding_extra",
        ),
        # The head would read the dumped computed field instead of the client's value
        pytest.param(_json_shadowed_by_computed_field_endpoint, {"raw": '{"a": 1}'}, id="key_of_computed_field"),
        pytest.param(
            _int_tree_in_json_endpoint, '{"x": 1, "children": [{"x": 2}]}', id="self_referencing_model_in_json"
        ),
        pytest.param(
            _json_tree_with_children_endpoint,
            {"data": '{"a": 1}', "children": [{"data": '{"b": 2}'}]},
            id="self_referencing_model_below_its_top",
        ),
    ],
)
def test__request_body_migration__with_json_value_cadwyn_cant_encode__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
    payload: Any,
):
    router.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json=payload)
    assert "json_type" in {error["type"] for error in exc_info.value.errors}


def test__request_body_migration__with_json_field_below_wide_and_deep_models__should_plan_it_quickly(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    # Each of the 6 levels refers to the next one 20 times, so walking every path would take 20 ** 6 steps
    model: type[BaseModel] = SchemaWithJsonDict
    for level in range(6):
        fields: dict[str, Any] = {f"field{i}": (Optional[model], None) for i in range(20)}  # ty: ignore[invalid-type-form]
        model = create_model(f"WideSchemaWithJsonDict{level}", **fields)

    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return body

    router.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change())

    started = time_module.perf_counter()
    response = clients["2000-01-01"].post(test_path, json={"field0": None})
    # The first request plans the body. It takes milliseconds, so this only fails if planning isn't linear.
    assert time_module.perf_counter() - started < 5
    assert response.status_code == 200


def _make_model_chain(model: type[BaseModel], payload: Any, depth: int) -> tuple[type[BaseModel], Any]:
    for i in range(depth):
        model = create_model(f"{model.__name__}Wrapper{i}", child=(model, ...))
        payload = {"child": payload}
    return model, payload


def _make_json_model_chain(depth: int, *, is_in_json: bool) -> tuple[type[BaseModel], Any]:
    """Return a model whose `Json[...]` field is `depth` models deep, or which has a `Json[...]` value that deep"""
    if not is_in_json:
        return _make_model_chain(SchemaWithJsonDict, {"data": '{"a": 1}'}, depth)
    inner_model, inner_payload = _make_model_chain(InnerOfJson, {"x": 1}, depth)
    model = create_model(f"JsonOf{inner_model.__name__}", data=(Json[inner_model], ...))  # ty: ignore[invalid-type-form]
    return model, {"data": json.dumps(inner_payload)}


@pytest.mark.parametrize("is_in_json", [False, True])
def test__request_body_migration__with_json_field_as_deep_as_cadwyn_plans__should_be_the_same_as_in_fastapi(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    is_in_json: bool,
):
    model, payload = _make_json_model_chain(7, is_in_json=is_in_json)

    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        return body

    router.post(test_path)(endpoint)
    fastapi_app = fastapi.FastAPI()
    fastapi_app.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change())

    expected = TestClient(fastapi_app).post(test_path, json=payload)
    response = clients["2000-01-01"].post(test_path, json=payload)
    assert (response.status_code, response.json()) == (expected.status_code, expected.json())


@pytest.mark.parametrize("is_in_json", [False, True])
def test__request_body_migration__with_json_field_deeper_than_cadwyn_plans__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    is_in_json: bool,
):
    model, payload = _make_json_model_chain(8, is_in_json=is_in_json)

    @router.post(test_path)
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        raise NotImplementedError

    clients = create_versioned_clients(version_change())

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json=payload)
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


@pytest.mark.parametrize(
    ("key", "expected_error_type"),
    [
        pytest.param("inner", "model_attributes_type", id="model"),
        pytest.param("items", "list_type", id="list"),
        pytest.param("by_key", "dict_type", id="dict"),
    ],
)
def test__request_body_migration__with_json_container_replaced_by_migration__should_fail_with_its_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    key: str,
    expected_error_type: str,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithNestedJsonDicts):
        raise NotImplementedError

    @convert_request_to_next_version_for(SchemaWithNestedJsonDicts)
    def migrator(request: RequestInfo):
        request.body[key] = "not a container"

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(
            test_path, json={"inner": {"data": "{}"}, "items": [{"data": "{}"}], "by_key": {"k": {"data": "{}"}}}
        )
    assert (expected_error_type, ("body", key)) in [(error["type"], error["loc"]) for error in exc_info.value.errors]


async def _raw_optional_json_body_with_default_endpoint(body: Annotated[Json[Optional[int]], Body()] = 23):
    return {"body": body}


@pytest.mark.skipif(  # pragma: no cover # FastAPI < 0.151
    FASTAPI_DISTINGUISHES_NULL_BODIES, reason="Since FastAPI 0.151.0, a body that is None isn't an omitted one"
)
def test__request_body_migration__with_raw_json_body_that_is_null__should_get_its_default_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    router.post(test_path)(_raw_optional_json_body_with_default_endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    assert clients["2000-01-01"].post(test_path, json="null").json() == {"body": 23}


@pytest.mark.skipif(  # pragma: no cover # FastAPI >= 0.151
    not FASTAPI_DISTINGUISHES_NULL_BODIES, reason="Before FastAPI 0.151.0, a body that is None is an omitted one"
)
def test__request_body_migration__with_raw_json_body_that_is_null__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    router.post(test_path)(_raw_optional_json_body_with_default_endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json="null")
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


class SchemaWithJsonAny(BaseModel):
    value: Json[Any]


class SchemaWithJsonAnyItemsAndJsonDict(BaseModel):
    items: list[Json[Any]]
    inner: SchemaWithJsonAny
    data: Json[dict[str, Any]]


@pytest.mark.parametrize(
    ("replace", "payload"),
    [
        # The only string is in the container that the migration replaces, so it's the only one we could miss
        pytest.param(
            lambda body: {**body, "items": tuple(body["items"])},
            {"items": ['"123"'], "inner": {"value": "1"}, "data": "{}"},
            id="list_with_tuple",
        ),
        pytest.param(
            lambda body: {**body, "inner": MappingProxyType(body["inner"])},
            {"items": ["1"], "inner": {"value": '"123"'}, "data": "{}"},
            id="dict_with_mapping",
        ),
    ],
)
def test__request_body_migration__with_json_container_of_another_type__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    replace: Callable[[dict[str, Any]], dict[str, Any]],
    payload: dict[str, Any],
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonAnyItemsAndJsonDict):
        raise NotImplementedError

    @convert_request_to_next_version_for(SchemaWithJsonAnyItemsAndJsonDict)
    def migrator(request: RequestInfo):
        request.body = replace(request.body)

    clients = create_versioned_clients(version_change(migrator=migrator))

    # The head would decode the string in the replaced container once more if we encoded the other values
    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json=payload)
    assert ("json_type", ("body", "data")) in [(error["type"], error["loc"]) for error in exc_info.value.errors]


async def _raw_json_list_of_ints_body_with_default_endpoint(
    body: Annotated[Json[list[int]], Body()] = ["1"],  # noqa: B006  # ty: ignore[invalid-parameter-default]
):
    raise NotImplementedError


async def _raw_json_any_body_with_default_endpoint(
    body: Annotated[Json[Any], Body()] = {1: "integer", "1": "text"},  # noqa: B006
):
    raise NotImplementedError


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param(_raw_json_list_of_ints_body_with_default_endpoint, id="default_of_another_type"),
        pytest.param(_raw_json_any_body_with_default_endpoint, id="default_that_isnt_json"),
    ],
)
def test__request_body_migration__with_json_body_default__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    endpoint: Callable[..., Coroutine[Any, Any, Any]],
):
    router.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change(), router=router)

    # FastAPI gives the param its default without validating it, so we don't know what the head would read from it
    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path)
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


class _MixedEnum(Enum):
    A = 1
    B = "1"


class _Color(Enum):
    DARK = "dark"


class _Shade(Enum):
    DARK = "dark"


_self_referencing_list: list[Any] = []
_self_referencing_list.append(_self_referencing_list)


@pytest.mark.parametrize(
    ("annotation", "payload", "value"),
    [
        # JSON keys are strings, so these keys collapse into each other
        pytest.param(dict[_MixedEnum, int], '{"1": 5}', {_MixedEnum.A: 5}, id="keys_of_enum_with_mixed_types"),
        pytest.param(dict[Union[int, str], int], '{"1": 5}', {1: 5, "1": 6}, id="keys_of_union"),
        pytest.param(dict[Union[bool, int], int], '{"1": 5}', {1: 5}, id="keys_of_bool_or_int"),
        pytest.param(
            dict[Literal[1, "1"], str], '{"1": "a"}', {1: "integer", "1": "string"}, id="keys_of_mixed_literal"
        ),
        pytest.param(dict[float, int], '{"1.5": 5}', {float("nan"): 1, float("-nan"): 2}, id="keys_that_collide"),
        # The head would look up a member of its own enum
        pytest.param(list[_Color], '["dark"]', [_Shade.DARK], id="member_of_another_enum"),
        # JSON offsets only have minutes, and JSON has no folds or time zones of other types
        pytest.param(
            dict[str, datetime],
            '{"a": "2020-01-01T00:00:00Z"}',
            {"a": datetime(2020, 1, 1, tzinfo=timezone(timedelta(seconds=561)))},
            id="datetime_with_offset_in_seconds",
        ),
        pytest.param(
            dict[str, datetime],
            '{"a": "2020-01-01T00:00:00Z"}',
            {"a": datetime(2020, 1, 1, tzinfo=timezone.utc)},
            id="datetime_with_time_zone_of_another_type",
        ),
        pytest.param(
            dict[str, datetime],
            '{"a": "2020-01-01T00:00:00"}',
            {"a": datetime(2020, 1, 1, fold=1)},  # noqa: DTZ001
            id="fold",
        ),
        pytest.param(
            dict[str, time],
            '{"a": "12:00:00Z"}',
            {"a": time(12, tzinfo=timezone(timedelta(seconds=561)))},
            id="time_with_offset_in_seconds",
        ),
        # Walking it must not hang
        pytest.param(Any, "[]", _self_referencing_list, id="self_referencing_value"),
        # JSON can't have lone surrogates, so encoding fails
        pytest.param(Any, "[]", ["\ud800"], id="lone_surrogate"),
        pytest.param(dict[str, int], '{"a": 1}', {"\ud800": 1}, id="key_with_lone_surrogate"),
    ],
)
def test__request_body_migration__with_json_value_that_encoding_would_change__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    annotation: Any,
    payload: str,
    value: Any,
):
    model = create_model("SchemaWithJsonValueSetByMigration", data=(Json[annotation], ...))

    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        raise NotImplementedError

    router.post(test_path)(endpoint)

    @convert_request_to_next_version_for(model)
    def migrator(request: RequestInfo):
        request.body["data"] = value

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": payload})
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


class SchemaWithJsonBytes(BaseModel):
    data: Json[dict[str, bytes]]


def test__request_body_migration__with_json_bytes_that_cant_be_encoded__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonBytes):
        raise NotImplementedError

    @convert_request_to_next_version_for(SchemaWithJsonBytes)
    def migrator(request: RequestInfo):
        request.body["data"] = {"k": b"\xff"}

    clients = create_versioned_clients(version_change(migrator=migrator))

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": '{"k": "a"}'})
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


class SchemaWithJsonDictOfInts(BaseModel):
    data: Json[dict[str, int]]


def _read_body_as_dict(body: dict[str, Any] = Body()) -> dict[str, Any]:
    return body


def test__request_body_migration__with_json_field_and_dependency_that_reads_the_body__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(
        body: SchemaWithJsonDictOfInts, from_dependency: Annotated[dict[str, Any], Depends(_read_body_as_dict)]
    ):
        raise NotImplementedError

    clients = create_versioned_clients(version_change())

    # The dependency would get the JSON that we encode, `{"a":1}`, instead of the client's
    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": '{"a": "1"}'})
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


# Cadwyn compares the defaults when it generates the versions, so we only make the comparisons raise after that
_comparisons_of_ints_raise = threading.Event()


class _IntThatCantBeCompared(int):
    @override
    def __eq__(self, other: object) -> bool:
        if _comparisons_of_ints_raise.is_set():
            raise RuntimeError("Can't compare")
        return super().__eq__(other)

    __hash__ = int.__hash__


class SchemaWithJsonDictAndDefaultThatCantBeCompared(BaseModel):
    data: Json[dict[str, Any]]
    other: int = _IntThatCantBeCompared(1)


def test__request_body_migration__with_json_field_whose_planning_fails__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonDictAndDefaultThatCantBeCompared):
        raise NotImplementedError

    # Comparing the core schemas of the versions compares the defaults, which raises
    clients = create_versioned_clients(
        version_change(
            schema(SchemaWithJsonDictAndDefaultThatCantBeCompared).field("other").had(default=_IntThatCantBeCompared(2))
        )
    )

    _comparisons_of_ints_raise.set()
    try:
        with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
            clients["2000-01-01"].post(test_path, json={"data": '{"a": 1}'})
    finally:
        _comparisons_of_ints_raise.clear()
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


def _make_int_that_counts_its_schemas(make_schema: Callable[[int], core_schema.CoreSchema]) -> Any:
    """Return an int type whose core schema `make_schema` makes from the number of schemas made since the reset"""
    builds = itertools.count(1)

    class IntThatCountsItsSchemas(int):
        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
            return make_schema(next(builds))

        @classmethod
        def reset(cls) -> None:
            nonlocal builds
            builds = itertools.count(1)

    return IntThatCountsItsSchemas


def _raise_on_second_build(build: int) -> core_schema.CoreSchema:
    if build == 2:
        raise HTTPException(status_code=503)
    return core_schema.int_schema()


@pytest.mark.parametrize(
    ("make_schema", "expected_status_code"),
    [
        # Each override is solved once per pass, so the schemas have `gt=1` and `gt=2`, and 3 passes both
        pytest.param(lambda build: core_schema.int_schema(gt=build), 200, id="schema_that_changes_per_build"),
        pytest.param(_raise_on_second_build, 503, id="schema_that_raises_on_second_build"),
    ],
)
def test__request_body_migration__with_omitted_json_field_and_overridden_dependency__should_solve_it_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    make_schema: Callable[[int], core_schema.CoreSchema],
    expected_status_code: int,
):
    """Nothing is encoded, so building the overrides once more to check the readers of the body would change them"""
    counted_int = _make_int_that_counts_its_schemas(make_schema)

    def get_limit(limit: int) -> int:
        raise NotImplementedError

    def get_overridden_limit(limit: counted_int) -> int:  # ty: ignore[invalid-type-form]
        return limit

    @router.post(test_path)
    async def endpoint(body: SchemaWithOptionalJsonDict, limit: Annotated[int, Depends(get_limit)]):
        return {"data": body.data, "limit": limit}

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[get_limit] = get_overridden_limit
    counted_int.reset()

    response = clients["2000-01-01"].post(test_path, params={"limit": 3}, json={})
    assert response.status_code == expected_status_code


@pytest.mark.parametrize(
    ("data", "expected_builds"),
    [
        # Each request solves the override in both passes, and checking the readers of the body builds it once more
        pytest.param('{"a": 1}', 3, id="value_to_encode"),
        pytest.param(None, 2, id="nothing_to_encode"),
    ],
)
def test__request_body_migration__with_json_field_and_overridden_dependency__should_build_it_once_more_if_it_encodes(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    data: Optional[str],
    expected_builds: int,
):
    builds: list[int] = []

    def make_schema(build: int) -> core_schema.CoreSchema:
        builds.append(build)
        return core_schema.int_schema()

    counted_int = _make_int_that_counts_its_schemas(make_schema)

    def get_limit(limit: int) -> int:
        raise NotImplementedError

    def get_overridden_limit(limit: counted_int) -> int:  # ty: ignore[invalid-type-form]
        return limit

    @router.post(test_path)
    async def endpoint(body: SchemaWithOptionalJsonDict, limit: Annotated[int, Depends(get_limit)]):
        return {"data": body.data, "limit": limit}

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[get_limit] = get_overridden_limit
    counted_int.reset()

    for _ in range(2):
        builds.clear()
        response = clients["2000-01-01"].post(test_path, params={"limit": 3}, json={"data": data})
        assert response.json() == {"data": None if data is None else json.loads(data), "limit": 3}
        assert len(builds) == expected_builds


def test__request_body_migration__with_override_that_reads_the_body_later__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    def get_value(value: int = 0) -> Any:
        raise NotImplementedError

    def get_overridden_value(**kwargs: Any) -> Any:
        return kwargs

    get_overridden_value.__signature__ = inspect.signature(get_value)  # ty: ignore[unresolved-attribute]

    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonDictOfInts, from_dependency: Annotated[Any, Depends(get_value)]):
        return from_dependency

    clients = create_versioned_clients(version_change())
    clients["2000-01-01"].app.dependency_overrides[get_value] = get_overridden_value
    assert clients["2000-01-01"].post(test_path, json={"data": '{"a": 1}'}).json() == {"value": 0}

    # FastAPI builds the override from its new signature, so it reads the body now, which we must check again
    get_overridden_value.__signature__ = inspect.signature(_read_body_as_dict)  # ty: ignore[unresolved-attribute]

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": '{"a": "1"}'})
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


class _KeyThatCountsItsHashes(str, Enum):
    A = "a"

    @override
    def __hash__(self) -> int:
        _key_hashes.append(self)
        return str.__hash__(self)


_key_hashes: list[_KeyThatCountsItsHashes] = []


class SchemaWithDictOfOptionalJsonInts(BaseModel):
    data: dict[_KeyThatCountsItsHashes, Optional[Json[int]]]


class SchemaWithDictOfOptionalInts(BaseModel):
    data: dict[_KeyThatCountsItsHashes, Optional[int]]


def test__request_body_migration__with_json_values_to_encode_none_of__should_use_the_body_as_without_them(
    create_versioned_clients: CreateVersionedClients,
    router: VersionedAPIRouter,
):
    """Nothing is encoded, so we must read the body without hashing its keys or calling anything else of users"""

    @router.post("/json")
    async def json_endpoint(body: SchemaWithDictOfOptionalJsonInts):
        return body

    @router.post("/plain")
    async def plain_endpoint(body: SchemaWithDictOfOptionalInts):
        return body

    clients = create_versioned_clients(version_change())

    hashes_by_path = {}
    for path in ("/json", "/plain"):
        _key_hashes.clear()
        assert clients["2000-01-01"].post(path, json={"data": {"a": None}}).json() == {"data": {"a": None}}
        hashes_by_path[path] = len(_key_hashes)
    assert hashes_by_path["/json"] == hashes_by_path["/plain"]


class SchemaWithJsonAnyData(BaseModel):
    data: Json[Any]


class SchemaWithOptionalJsonInt(BaseModel):
    data: Optional[Json[int]] = None


def test__request_body_migration__with_override_that_changes_the_body_when_built__should_get_the_body_it_changes(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    """Checking the dependencies builds the override, whose schema hook changes the body before we encode it"""
    migrated_bodies: list[dict[str, Any]] = []

    class IntThatChangesTheBody(int):
        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
            if migrated_bodies:
                migrated_bodies[-1]["data"] = '{"a": 1}'
            return core_schema.int_schema()

    def get_limit(limit: int = 1) -> int:
        raise NotImplementedError

    def get_overridden_limit(limit: IntThatChangesTheBody = 1) -> int:  # ty: ignore[invalid-parameter-default]
        return limit

    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonAnyData, limit: Annotated[int, Depends(get_limit)]):
        return {"data": body.data, "type": type(body.data).__name__}

    @convert_request_to_next_version_for(SchemaWithJsonAnyData)
    def migrator(request: RequestInfo):
        migrated_bodies.append(request.body)

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[get_limit] = get_overridden_limit

    # The head parses the JSON that the hook has set, as it does without the fix, instead of the JSON that we encode
    response = clients["2000-01-01"].post(test_path, json={"data": '{"b": 2}'})
    assert response.json() == {"data": {"a": 1}, "type": "dict"}


def test__request_body_migration__with_key_whose_equality_runs_code__should_compare_it_as_before(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    """Looking up the keys of a model compares them to the keys of the data, which can run code of users"""
    equalities: list[object] = []

    class StringThatCountsItsEqualities(str):
        __slots__ = ()
        __hash__ = str.__hash__

        @override
        def __eq__(self, other: object) -> bool:
            equalities.append(other)
            return str.__eq__(self, other)

    @router.post(test_path)
    async def endpoint(body: SchemaWithOptionalJsonInt):
        return {"data": body.data}

    @convert_request_to_next_version_for(SchemaWithOptionalJsonInt)
    def migrator(request: RequestInfo):
        request.body = {StringThatCountsItsEqualities("data"): None}
        equalities.clear()

    clients = create_versioned_clients(version_change(migrator=migrator))

    # The head looks the key up at most once, as it does without the fix, and we don't look it up at all
    assert clients["2000-01-01"].post(test_path, json={}).json() == {"data": None}
    assert len(equalities) <= 1


class _InnerWithX(BaseModel):
    x: int


class SchemaWithJsonModelAndMarker(BaseModel):
    data: Json[_InnerWithX]
    marker: int


def test__request_body_migration__with_json_model_whose_keys_compare_with_code__should_fail_without_comparing_them(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    """Checking the value of a `Json[Model]` compares its keys, which could run code of users that changes the body"""
    migrated_bodies: list[dict[str, Any]] = []

    class KeyThatChangesTheBody(str):
        __slots__ = ()
        __hash__ = str.__hash__

        @override
        def __eq__(self, other: object) -> bool:  # pragma: no cover # Cadwyn mustn't compare the key
            migrated_bodies[-1]["marker"] = 99
            return str.__eq__(self, other)

    class IntThatChangesTheBody(int):
        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
            if migrated_bodies:
                migrated_bodies[-1]["data"] = {KeyThatChangesTheBody("x"): 7}
            return core_schema.int_schema()

    def get_limit(limit: int = 1) -> int:
        raise NotImplementedError

    def get_overridden_limit(limit: IntThatChangesTheBody = 1) -> int:  # ty: ignore[invalid-parameter-default]
        return limit

    @router.post(test_path)
    async def endpoint(body: SchemaWithJsonModelAndMarker, limit: Annotated[int, Depends(get_limit)]):
        raise NotImplementedError

    @convert_request_to_next_version_for(SchemaWithJsonModelAndMarker)
    def migrator(request: RequestInfo):
        migrated_bodies.append(request.body)

    clients = create_versioned_clients(version_change(migrator=migrator))
    clients["2000-01-01"].app.dependency_overrides[get_limit] = get_overridden_limit

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": '{"x": 1}', "marker": 1})
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]
    assert migrated_bodies[-1]["marker"] == 1


class SchemaWithDictOfJsonIntsByKeysThatCountTheirHashes(BaseModel):
    data: dict[_KeyThatCountsItsHashes, Json[int]]


def test__request_body_migration__with_json_values_by_keys_that_hash_with_code__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
):
    """Copying the dict to encode its values would hash its keys once more, which runs their code"""

    @router.post(test_path)
    async def endpoint(body: SchemaWithDictOfJsonIntsByKeysThatCountTheirHashes):
        raise NotImplementedError

    clients = create_versioned_clients(version_change())

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json={"data": {"a": "1"}})
    assert [error["type"] for error in exc_info.value.errors] == ["json_type"]


class SchemaWithJsonModel(BaseModel):
    data: Json[InnerOfJson]


class SchemaWithJsonDictAndOtherDict(BaseModel):
    data: Json[dict[str, Any]]
    other: dict[str, Any] = {}


class SchemaWithJsonDictAndBoolOrOne(BaseModel):
    data: Json[dict[str, Any]]
    other: Union[bool, Literal[1]] = False


class SchemaWithJsonDictOfLowerStrings(BaseModel):
    data: Json[dict[str, Annotated[str, StringConstraints(to_lower=True)]]]


@pytest.mark.parametrize(
    ("model", "instruction", "payload"),
    [
        # Without migrations, the head would give `x` its default instead of the client's value
        pytest.param(
            SchemaWithJsonModel,
            schema(InnerOfJson).field("x").had(name="old_x"),
            {"data": '{"old_x": 5}'},
            id="renamed_field_in_json",
        ),
        pytest.param(
            SchemaWithJsonModel,
            schema(InnerOfJson).field("x").had(exclude=True),
            {"data": '{"x": 5}'},
            id="excluded_field_in_json",
        ),
        pytest.param(
            SchemaWithJsonModel,
            schema(InnerOfJson).field("x").had(serialization_alias="y"),
            {"data": '{"x": 5}'},
            id="serialization_alias_in_json",
        ),
        # The head would read the dumped value of `other` for `data`
        pytest.param(
            SchemaWithJsonDictAndOtherDict,
            schema(SchemaWithJsonDictAndOtherDict).field("other").had(serialization_alias="data"),
            {"data": '{"a": 1}', "other": {"b": 2}},
            id="serialization_alias_of_another_field",
        ),
        # `True == 1` in Python, but the older version validates `1` into `True`
        pytest.param(
            SchemaWithJsonDictAndBoolOrOne,
            schema(SchemaWithJsonDictAndBoolOrOne).field("other").had(type=Union[bool, Literal[True]]),  # noqa: PYI051
            {"data": "{}", "other": 1},
            id="literal_of_another_type",
        ),
        # The older version changes the strings, and the head changes them once more
        pytest.param(
            SchemaWithJsonDictOfLowerStrings,
            schema(SchemaWithJsonDictOfLowerStrings)
            .field("data")
            .had(type=Json[dict[str, Annotated[str, StringConstraints(to_upper=True, strip_whitespace=True)]]]),
            {"data": '{"k": " aBc "}'},
            id="constraint_of_string_in_json",
        ),
        # The head would give the sibling its default instead of the client's value
        pytest.param(
            SchemaWithJsonDictAndOtherDict,
            schema(SchemaWithJsonDictAndOtherDict).field("other").had(exclude=True),
            {"data": '{"a": 1}', "other": {"b": 2}},
            id="excluded_sibling",
        ),
    ],
)
def test__request_body_migration__with_json_field_dumped_differently_in_older_version__should_fail_with_json_type_error(
    create_versioned_clients: CreateVersionedClients,
    test_path: Literal["/test"],
    router: VersionedAPIRouter,
    model: type[BaseModel],
    instruction: Any,
    payload: dict[str, Any],
):
    async def endpoint(body: model):  # ty: ignore[invalid-type-form]
        raise NotImplementedError

    router.post(test_path)(endpoint)
    clients = create_versioned_clients(version_change(instruction))

    with pytest.raises(CadwynHeadRequestValidationError) as exc_info:
        clients["2000-01-01"].post(test_path, json=payload)
    assert [(error["type"], error["loc"]) for error in exc_info.value.errors] == [("json_type", ("body", "data"))]


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
