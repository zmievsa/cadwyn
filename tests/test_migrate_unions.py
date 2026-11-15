"""`migrate_request()` and `migrate_response()` migrate unions of models as routes do."""

from typing import Annotated, Any, Literal, Optional, Union

import pytest
from pydantic import BaseModel, Field

from cadwyn import (
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    migrate_request,
    migrate_response,
    schema,
)
from cadwyn.exceptions import CadwynError
from tests.conftest import CadwynTestClient, CreateVersionedApp, version_change


class _User(BaseModel):
    kind: Literal["user"] = "user"
    full_name: str


class _Group(BaseModel):
    kind: Literal["group"] = "group"
    size: int


class _Unchanged(BaseModel):
    kind: Literal["unchanged"] = "unchanged"


@convert_request_to_next_version_for(_User)
def _rename_name_to_full_name(request: RequestInfo):
    request.body["full_name"] = request.body.pop("name")


@convert_response_to_previous_version_for(_User)
def _rename_full_name_to_name(response: ResponseInfo):
    response.body["name"] = response.body.pop("full_name")


@convert_request_to_next_version_for(_Group)
def _count_in_dozens(request: RequestInfo):
    request.body["size"] *= 12


@convert_response_to_previous_version_for(_Group)
def _count_in_units(response: ResponseInfo):
    response.body["size"] //= 12


def _get_version_change():
    return version_change(
        schema(_User).field("full_name").had(name="name"),
        rename_name_to_full_name=_rename_name_to_full_name,
        rename_full_name_to_name=_rename_full_name_to_name,
        count_in_dozens=_count_in_dozens,
        count_in_units=_count_in_units,
    )


def _get_versions() -> VersionBundle:
    return VersionBundle(Version("2001-01-01", _get_version_change()), Version("2000-01-01"))


_UNIONS = [
    pytest.param(Union[_User, _Group], id="union"),
    pytest.param(_User | _Group, id="union_type"),
    pytest.param(Annotated[Union[_User, _Group], Field(discriminator="kind")], id="discriminated"),
]


@pytest.mark.parametrize("annotation", _UNIONS)
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param({"kind": "user", "name": "John"}, _User(full_name="John"), id="user"),
        pytest.param({"kind": "group", "size": 2}, _Group(size=24), id="group"),
    ],
)
def test__migrate_request__of_union__should_run_the_migrations_of_the_member_that_the_body_is(
    annotation: Any, body: dict[str, Any], expected: BaseModel
):
    assert migrate_request(_get_versions(), annotation, body=body, version="2000-01-01").body == expected


def test__migrate_request__of_optional_model__without_body__should_give_none():
    assert migrate_request(_get_versions(), Optional[_User], body=None, version="2000-01-01").body is None


def test__migrate_request__of_union__to_version__should_give_the_member_of_that_version():
    versions = VersionBundle(
        Version("2002-01-01", version_change()), Version("2001-01-01", _get_version_change()), Version("2000-01-01")
    )

    migrated = migrate_request(
        versions,
        Union[_User, _Group],
        body={"kind": "user", "name": "John"},
        version="2000-01-01",
        to_version="2001-01-01",
    ).body

    assert migrated.model_dump() == {"kind": "user", "full_name": "John"}
    assert type(migrated).__cadwyn_original_model__ is _User


def test__migrate_request__of_union_migration__should_replace_the_migrations_of_its_members():
    @convert_request_to_next_version_for(Union[_User, _Group])
    def migrate_union(request: RequestInfo):
        request.body["full_name"] = "From the union"

    versions = VersionBundle(
        Version("2001-01-01", version_change(migrate_union=migrate_union, migrate_user=_rename_name_to_full_name)),
        Version("2000-01-01"),
    )

    migrated = migrate_request(
        versions, Union[_User, _Group], body={"kind": "user", "full_name": "John"}, version="2000-01-01"
    ).body

    assert migrated == _User(full_name="From the union")


def _create_app_with_union_routes(
    create_versioned_app: CreateVersionedApp, router: VersionedAPIRouter, *, response_body: BaseModel
):
    """Every migration needs a route that it applies to, so the app has a route of a union body and one of a response"""

    @router.post("/request")
    async def take_union(item: Union[_User, _Group]):
        return {"item": repr(item)}

    @router.get("/response", response_model=Union[_User, _Group])
    async def give_union():
        return response_body

    return create_versioned_app(_get_version_change())


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"kind": "user", "name": "John"}, id="user"),
        pytest.param({"kind": "group", "size": 2}, id="group"),
    ],
)
def test__migrate_request__of_union__should_give_what_the_endpoint_of_a_route_gets(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
    body: dict[str, Any],
):
    app = _create_app_with_union_routes(create_versioned_app, router, response_body=_User(full_name="John"))
    route_result = CadwynTestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"}).post(
        "/request", json=body
    )
    assert route_result.status_code == 200

    migrated = migrate_request(app.versions, Union[_User, _Group], body=body, version="2000-01-01").body

    assert {"item": repr(migrated)} == route_result.json()


@pytest.mark.parametrize("annotation", _UNIONS)
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(_User(full_name="John"), {"kind": "user", "name": "John"}, id="user"),
        pytest.param(_Group(size=24), {"kind": "group", "size": 2}, id="group"),
    ],
)
def test__migrate_response__of_union__should_run_the_migrations_of_the_member_that_the_body_is(
    annotation: Any, body: BaseModel, expected: dict[str, Any]
):
    migrated = migrate_response(_get_versions(), annotation, body=body, version="2000-01-01").body

    assert migrated.model_dump() == expected
    assert type(migrated).__cadwyn_original_model__ is type(body)


def test__migrate_response__of_optional_model__without_body__should_give_none():
    assert migrate_response(_get_versions(), Optional[_User], body=None, version="2000-01-01").body is None


def test__migrate_response__of_union__from_version__should_tell_the_member_by_the_model_of_that_version():
    versions = VersionBundle(
        Version("2002-01-01", version_change()), Version("2001-01-01", _get_version_change()), Version("2000-01-01")
    )
    body = migrate_request(
        versions,
        Union[_User, _Group],
        body={"kind": "user", "name": "John"},
        version="2000-01-01",
        to_version="2001-01-01",
    ).body

    migrated = migrate_response(
        versions, Union[_User, _Group], body=body, version="2000-01-01", from_version="2001-01-01"
    ).body

    assert migrated.model_dump() == {"kind": "user", "name": "John"}


def test__migrate_response__of_union__with_data_instead_of_object__should_raise_cadwyn_error():
    with pytest.raises(CadwynError, match=r"can't tell which member of .* the body is because it's a dict object"):
        migrate_response(
            _get_versions(), Union[_User, _Group], body={"kind": "user", "full_name": "John"}, version="2000-01-01"
        )


def test__migrate_response__of_union_whose_members_have_no_migrations__with_data__should_validate_it():
    migrated = migrate_response(
        _get_versions(), Union[_Unchanged, int], body={"kind": "unchanged"}, version="2000-01-01"
    ).body

    assert migrated.model_dump() == {"kind": "unchanged"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_User(full_name="John"), id="user"),
        pytest.param(_Group(size=24), id="group"),
    ],
)
def test__migrate_response__of_union__should_give_what_a_route_responds_with(
    create_versioned_app: CreateVersionedApp,
    router: VersionedAPIRouter,
    body: BaseModel,
):
    app = _create_app_with_union_routes(create_versioned_app, router, response_body=body)
    route_result = CadwynTestClient(app, headers={app.router.api_version_parameter_name: "2000-01-01"}).get("/response")
    assert route_result.status_code == 200

    migrated = migrate_response(app.versions, Union[_User, _Group], body=body, version="2000-01-01").body

    assert migrated.model_dump(mode="json") == route_result.json()


class _First(BaseModel):
    x: int


class _Second(BaseModel):
    x: int


@convert_request_to_next_version_for(_First)
def _add_one(request: RequestInfo):
    request.body["x"] += 1


@convert_request_to_next_version_for(_Second)
def _add_hundred(request: RequestInfo):
    request.body["x"] += 100


@convert_response_to_previous_version_for(_First)
def _subtract_one(response: ResponseInfo):
    response.body["x"] -= 1


@convert_response_to_previous_version_for(_Second)
def _subtract_hundred(response: ResponseInfo):
    response.body["x"] -= 100


def _get_versions_of_members_that_accept_the_same_body() -> VersionBundle:
    return VersionBundle(
        Version(
            "2001-01-01",
            version_change(
                add_one=_add_one,
                add_hundred=_add_hundred,
                subtract_one=_subtract_one,
                subtract_hundred=_subtract_hundred,
            ),
        ),
        Version("2000-01-01"),
    )


def test__migrate_request__of_unions_with_members_in_other_order__should_validate_into_their_own_first_member():
    # Both members accept the body, so validation gives the first one, and each union has its own first member
    versions = _get_versions_of_members_that_accept_the_same_body()

    first = migrate_request(versions, _First | _Second, body={"x": 1}, version="2000-01-01").body
    second = migrate_request(versions, _Second | _First, body={"x": 1}, version="2000-01-01").body

    assert (first, second) == (_First(x=2), _Second(x=101))


def test__migrate_response__of_unions_with_members_in_other_order__should_validate_into_their_own_first_member():
    versions = _get_versions_of_members_that_accept_the_same_body()

    first = migrate_response(versions, _First | _Second, body=_First(x=2), version="2000-01-01").body
    second = migrate_response(versions, _Second | _First, body=_Second(x=101), version="2000-01-01").body

    assert (type(first).__cadwyn_original_model__, first.x) == (_First, 1)
    assert (type(second).__cadwyn_original_model__, second.x) == (_Second, 1)


@pytest.mark.parametrize("migrate", [migrate_request, migrate_response])
def test__migrate_request_and_response__of_unhashable_annotation__should_raise_cadwyn_error(migrate: Any):
    annotation = Annotated[Union[_User, _Group], {"description": "Unhashable"}]

    with pytest.raises(CadwynError, match=r"can only migrate hashable annotations outside of routes"):
        migrate(_get_versions(), annotation, body={"kind": "group", "size": 1}, version="2000-01-01")


def test__migrate_response__of_union__with_object_of_other_model__should_raise_cadwyn_error():
    with pytest.raises(CadwynError, match=r"the body is because it's a _Unchanged object"):
        migrate_response(_get_versions(), Union[_User, _Group], body=_Unchanged(), version="2000-01-01")


@pytest.mark.parametrize("migrate", [migrate_request, migrate_response])
def test__migrate_request_and_response__of_list_of_models__should_raise_cadwyn_error(migrate: Any):
    with pytest.raises(CadwynError, match=r"\(\)` takes a Pydantic model or a union of them, not list\[.*_User\]\.$"):
        migrate(_get_versions(), list[_User], body=[], version="2000-01-01")
