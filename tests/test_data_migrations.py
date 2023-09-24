import http.cookies
import re
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from datetime import date
from types import ModuleType
from typing import Any, Literal

import pytest
from dirty_equals import IsPartialDict, IsStr
from fastapi import Body, Cookie, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from tests.conftest import (
    CreateVersionedClients,
    client,
    version_change,
)
from universi import VersionedAPIRouter
from universi.exceptions import UniversiStructureError
from universi.structure import (
    VersionChange,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
)
from universi.structure.data import RequestInfo, ResponseInfo


@pytest.fixture()
def test_path():
    return "/test"


@pytest.fixture()
def _get_endpoint(test_path: str, router: VersionedAPIRouter, latest_module: ModuleType):
    @router.get(test_path, response_model=latest_module.AnyResponseSchema)
    async def get_endpoint(request: Request):
        return {
            "body": await request.body(),
            "headers": request.headers,
            "cookies": request.cookies,
            "query_params": request.query_params,
        }

    return get_endpoint


@pytest.fixture()
def _post_endpoint(test_path: str, router: VersionedAPIRouter, latest_module: ModuleType):
    @router.post(test_path, response_model=latest_module.AnyResponseSchema)
    async def post_endpoint(request: Request, body: latest_module.AnyRequestSchema):
        return {
            "body": body.__root__,
            "headers": request.headers,
            "cookies": request.cookies,
            "query_params": request.query_params,
        }

    return post_endpoint


@pytest.fixture(params=["by path", "by schema"])
def version_change_1(request, test_path: str, latest_module):
    if request.param == "by path":
        convert_request = convert_request_to_next_version_for(test_path, {"POST"})
        convert_response = convert_response_to_previous_version_for(test_path, {"POST"})
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
def _post_endpoint_with_extra_depends(
    request: pytest.FixtureRequest,
    router: VersionedAPIRouter,
    test_path: Literal["/test"],
    latest_module: ModuleType,
    _post_endpoint: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
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
            return {
                "body": body.__root__,
                "headers": headers,
                "cookies": cookies,
                "query_params": query_params,
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

        # insert_assert(clients[date(2000, 1, 1)].post(test_path, json={}).json())
        assert clients[date(2000, 1, 1)].post(test_path, json={}).json() == {
            "body": {"hello": "hello"},
            "headers": IsPartialDict({"header_key": "header val 2"}),
            "cookies": {"cookie_key": "cookie val 2"},
            "query_params": {"query_param_key": "query_param val 2"},
        }
        # insert_assert(clients[date(2000, 1, 1)] .post(test_path, json={"1": "2"}, headers={"3": "4"}, cookies={"5": "6"}, params={"7": "8"}) .json())
        assert clients[date(2000, 1, 1)].post(
            test_path,
            json={"1": "2"},
            headers={"3": "4"},
            cookies={"5": "6"},
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
                "headers": request.headers,
                "cookies": request.cookies,
                "query_params": request.query_params,
            }

        @convert_request_to_next_version_for(test_path, {"GET"})
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
        router,
        test_path,
    ):
        @router.get(test_path)
        async def get(my_header: str = Header()):
            return 83

        @convert_request_to_next_version_for(test_path, {"GET"})
        def migrator(request: RequestInfo):
            del request.headers["my-header"]

        clients = create_versioned_clients(version_change(migrator=migrator))
        assert clients[date(2000, 1, 1)].get(test_path, headers={"my-header": "wow"}).json() == {
            "detail": [{"loc": ["header", "my-header"], "msg": "field required", "type": "value_error.missing"}],
        }
        assert clients[date(2001, 1, 1)].get(test_path, headers={"my-header": "wow"}).json() == 83

    def test__optional_body_field(
        self,
        create_versioned_clients: CreateVersionedClients,
        latest_module: ModuleType,
        test_path: Literal["/test"],
        router,
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
        # insert_assert(resp.json())
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
            },
            "cookies": {},
            "query_params": {},
            "body_key": "body_val",
        }
        assert dict(resp.headers) == {
            "content-length": "252",
            "content-type": "application/json",
            "header": "header_val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
        }
        assert dict(resp.cookies) == {"cookie_key": "cookie_val"}
        assert resp.status_code == 300

        resp = clients[date(2000, 1, 1)].post(test_path, json={"1": "2"}, headers={"3": "4"}, cookies={"5": "6"})
        assert resp.json() == {
            "body": {"1": "2"},
            "headers": {
                "host": "testserver",
                "accept": "*/*",
                "accept-encoding": "gzip, deflate",
                "connection": "keep-alive",
                "user-agent": "testclient",
                "3": "4",
                "cookie": "5=6; cookie_key=cookie_val",
                "content-length": "10",
                "content-type": "application/json",
            },
            "cookies": {"5": "6", "cookie_key": "cookie_val"},
            "query_params": {},
            "body_key": "body_val",
        }
        assert dict(resp.headers) == {
            "content-length": "339",
            "content-type": "application/json",
            "header": "header_val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
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
            "content-length": "175",
            "content-type": "application/json",
            "header_key": "header-val",
            "set-cookie": "cookie_key=cookie_val; Max-Age=83; Path=/; SameSite=lax",
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
            {"header-key": "header-val2", "content-length": "20", "content-type": "application/json"}
        )
        assert resp.status_code == 201
        assert dict(resp.cookies) == {}

        resp = clients[date(2001, 1, 1)].post(test_path, json={})
        assert resp.json() == {"hewwo": "darkness"}
        assert dict(resp.headers) == (
            {"header-key": "header-val", "content-length": "20", "content-type": "application/json"}
        )
        assert resp.status_code == 301


class TestHowAndWhenMigrationsApply:
    def test__migrate__with_no_migrations__should_not_raise_error(
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
        none_client = client(clients[date(2000, 1, 1)].app.router, api_version=None, api_version_var=api_version_var)
        assert none_client.post(test_path, json=[]).json()["body"] == []

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
        earlier_client = client(
            clients[date(2000, 1, 1)].app.router,
            api_version=date(1998, 2, 10),
            api_version_var=api_version_var,
        )
        assert earlier_client.post(test_path, json=[]).json()["body"] == [
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
        later_client = client(
            clients[date(2000, 1, 1)].app.router,
            api_version=date(5000, 1, 1),
            api_version_var=api_version_var,
        )
        assert later_client.post(test_path, json=[]).json()["body"] == []

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
                    wrong_req_path=convert_request_to_next_version_for("/wrong_path", {"POST"})(bad_req),
                    wrong_req_method=convert_request_to_next_version_for(test_path, {"GET"})(bad_req),
                    wrong_resp_path=convert_response_to_previous_version_for("/wrong_path", {"POST"})(bad_resp),
                    wrong_resp_method=convert_response_to_previous_version_for(test_path, {"GET"})(bad_resp),
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
        # http.cookies.SimpleCookie(resp_2000.headers["set-cookie"])

        # insert_assert(dict(resp_2000.cookies))
        assert dict(resp_2000.cookies) == {"cookie_key": "cookie_val"}
        # insert_assert(dict(resp_2000.headers))
        assert dict(resp_2000.headers) == {
            "content-length": "2",
            "content-type": "application/json",
            "set-cookie": IsStr(),
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
        }


def test__invalid_path_migration_syntax():
    with pytest.raises(
        ValueError,
        match=re.escape("If path was provided as a first argument, methods must be provided as a second argument"),
    ):
        convert_request_to_next_version_for("/test")


def test__invalid_schema_migration_syntax(latest_module):
    with pytest.raises(
        ValueError,
        match=re.escape("If schema was provided as a first argument, methods argument should not be provided"),
    ):
        convert_request_to_next_version_for(latest_module.AnyRequestSchema, {"POST"})


def test__defining_two_migrations_for_the_same_request(latest_module):
    with pytest.raises(
        UniversiStructureError,
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
        UniversiStructureError,
        match=re.escape('There already exists a response migration for "AnyResponseSchema" in "MyVersionChange".'),
    ):

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migration1(response: ResponseInfo):
            raise NotImplementedError

        @convert_response_to_previous_version_for(latest_module.AnyResponseSchema)
        def migration2(response: ResponseInfo):
            raise NotImplementedError

        version_change(migration1=migration1, migration2=migration2)
