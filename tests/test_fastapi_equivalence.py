"""Untouched routes compared with plain FastAPI.

A route is untouched in a version if no version change between that version and head touches it: no endpoint
instruction of a newer version changes it, no migration of a newer version runs for it, it validates no models or
enums that differ from head, and no dependency asks for `current_dependency_solver`. Route generation records this on
every route that it generates. Cadwyn 10.0 will serve such routes with their head routes, so they must behave
exactly like plain FastAPI.

Every case has a route, a request, and an app with two versions whose version change touches something else. We send
the request to plain FastAPI and to each version of Cadwyn in which the route is untouched, and compare what they
return and what the endpoint, its dependencies, and its background tasks did.
"""

import dataclasses
from collections.abc import Callable, Iterable
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Union

import pytest
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    Json,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import override

from cadwyn import (
    Cadwyn,
    HeadVersion,
    RequestInfo,
    ResponseInfo,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    convert_request_to_next_version_for,
    convert_response_to_previous_version_for,
    endpoint,
    enum,
    schema,
)
from cadwyn.dependencies import current_dependency_solver
from cadwyn.route_generation import _UNTOUCHED_ROUTE_ATTR
from tests.conftest import version_change

if TYPE_CHECKING:
    _OnlyForTypeCheckers = int

_NEWER_VERSION = "2001-01-01"
_OLDER_VERSION = "2000-01-01"
_VERSIONS = (_NEWER_VERSION, _OLDER_VERSION)
_PATH = "/test"

# The cases whose untouched routes behave differently from plain FastAPI today: the case's id, and why.
#
# Cadwyn should not be stricter or safer than FastAPI or Pydantic, and it should not try to fix their shortcomings
# (AGENTS.md). This test enforces that for the routes that no version change touches, so this list may only shrink:
# a change must not add an entry to it. When a fix makes a case behave like FastAPI, the test fails until you remove
# the case's entry.
_FASTAPI_DEVIATIONS: dict[str, str] = {
    "json_in_request_body_with_type_field": "Json[...] request fields are decoded twice next to a `type` field (#677)",
    "json_in_request_body_named_metadata": "Json[...] request fields named `metadata` are decoded twice (#679)",
    "json_string_in_response_model": "Json[...] fields of returned models are decoded twice (#653)",
    "json_object_in_response_model": "Json[...] fields of returned models are decoded twice, which fails (#653)",
    "computed_field_in_response_model_that_forbids_extra": (
        "The dump of the returned model has the computed field, which its response model forbids as input (#678)"
    ),
    "overridden_model_dump_in_request_body": (
        "Computed fields reach head as input when the body's model overrides `model_dump()` (#570)"
    ),
    "strict_response_model_with_versioned_enum": "Strict models reject the members of versioned enums (#605)",
    "versioned_enum_in_iterable_request_body": (
        "Head rejects the versioned enum members that the request's version put in an `Iterable` body"
    ),
    "non_idempotent_validator_in_request_body": "Requests are validated twice: by their version and by head",
    "non_idempotent_validator_in_response_model": "Returned models are validated again by the response model (#592)",
    "before_model_validator_in_request_body": "Head validates the dump of the validated body instead of what was sent",
    "model_construct_with_wrong_type_in_response": (
        "Returned models are validated again by the response model, which coerces their values (#592)"
    ),
    "field_serializer_in_request_body": "Head validates the dump of the body, which serializers have changed",
    "field_serializer_in_response_model": (
        "Returned models are dumped and validated again, after serializers have changed the dump (#592)"
    ),
    "list_of_models_in_request_body_with_dependency_overrides": (
        "List and dict bodies lose extra keys and set fields in apps with dependency overrides (#566)"
    ),
    "union_with_serialization_alias_in_request_body": (
        "Head validates the dump of a union body by its serialization aliases"
    ),
    "dependency_with_side_effect": "Dependencies run twice: for the request's version and for head (#220)",
    "background_task_of_dependency_with_tasks_param": (
        "Dependencies run twice, so the background tasks that they add run twice (#220, #568)"
    ),
}

# What the endpoint, its dependencies, its validators, and its background tasks did during a request, in order
_events: list[str] = []


class _Unrelated(BaseModel):
    """The model that the version changes of the cases change, which none of their routes use"""

    a: int = 0
    b: int = 0


class _UnrelatedEnum(Enum):
    a = "a"


def _unrelated_change() -> type[VersionChange]:
    return version_change(schema(_Unrelated).field("b").didnt_exist)


@dataclasses.dataclass(frozen=True)
class _Case:
    id: str
    # Adds the case's single route at _PATH to a router, the same for FastAPI and for Cadwyn
    register: Callable[[APIRouter], None]
    request: dict[str, Any] = dataclasses.field(default_factory=dict)
    change: Callable[[], type[VersionChange]] = _unrelated_change
    # The versions in which the case's route is untouched, which we compare with FastAPI
    untouched_versions: tuple[str, ...] = _VERSIONS
    # Any overrides make Cadwyn handle some bodies differently, so the apps of some cases have one
    dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _Result:
    status_code: int
    body: Any
    events: list[str]


def _send(client: TestClient, case: _Case, headers: dict[str, str]) -> _Result:
    _events.clear()
    request = dict(case.request)
    response = client.post(_PATH, headers={**request.pop("headers", {}), **headers}, **request)
    if response.headers.get("content-type") == "application/json":
        body = response.json()
    else:
        body = response.text
    return _Result(response.status_code, body, list(_events))


def _create_apps(case: _Case, *head_changes: type[VersionChange]) -> tuple[FastAPI, Cadwyn]:
    fastapi_router = APIRouter()
    case.register(fastapi_router)
    fastapi_app = FastAPI()
    fastapi_app.include_router(fastapi_router)
    fastapi_app.dependency_overrides.update(case.dependency_overrides)
    cadwyn_router = VersionedAPIRouter()
    case.register(cadwyn_router)
    app = Cadwyn(
        versions=VersionBundle(
            HeadVersion(*head_changes), Version(_NEWER_VERSION, case.change()), Version(_OLDER_VERSION)
        )
    )
    app.generate_and_include_versioned_routers(cadwyn_router)
    app.dependency_overrides.update(case.dependency_overrides)
    app._cadwyn_initialize()
    return fastapi_app, app


def _get_untouched_versions(app: Cadwyn) -> tuple[str, ...]:
    return tuple(
        version
        for version, router in app.router.versioned_routers.items()
        for route in router.routes
        if isinstance(route, APIRoute) and getattr(route, _UNTOUCHED_ROUTE_ATTR)
    )


def _echo_model(model: BaseModel) -> dict[str, Any]:
    return {"dump": model.model_dump(), "fields_set": sorted(model.model_fields_set)}


# Json fields


class _JsonAnyBody(BaseModel):
    data: Json[Any]


def _json_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _JsonAnyBody):
        return {"data": body.data, "type": type(body.data).__name__}


class _JsonDictBody(BaseModel):
    data: Json[dict[str, int]]


def _json_object_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _JsonDictBody):
        return body.model_dump()


class _JsonBodyWithTypeField(BaseModel):
    type: str = ""
    data: Json[Any]


def _json_in_request_body_with_type_field(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _JsonBodyWithTypeField):
        return {"data": body.data, "type": type(body.data).__name__}


class _JsonBodyNamedMetadata(BaseModel):
    metadata: Json[Any]


def _json_in_request_body_named_metadata(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _JsonBodyNamedMetadata):
        return {"data": body.metadata, "type": type(body.metadata).__name__}


def _json_string_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_JsonAnyBody)
    async def route():
        return _JsonAnyBody(data='"123"')


def _json_object_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_JsonAnyBody)
    async def route():
        return _JsonAnyBody(data='{"a": 1}')


def _json_form_field(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(data: Annotated[Json[Any], Form()]):
        return {"data": data, "type": type(data).__name__}


# Computed fields


class _ForbiddingModelWithComputedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @computed_field
    @property
    def upper(self) -> str:
        return self.name.upper()


def _computed_field_in_response_model_that_forbids_extra(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ForbiddingModelWithComputedField)
    async def route():
        return _ForbiddingModelWithComputedField(name="a")


def _computed_field_in_request_body_that_forbids_extra(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ForbiddingModelWithComputedField):
        return body.model_dump()


class _ModelWithOverriddenDump(BaseModel):
    """Like SQLModel models, which override `model_dump()`"""

    name: str

    @computed_field
    @property
    def upper(self) -> str:
        return self.name.upper()

    @override
    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        return super().model_dump(**kwargs)


def _overridden_model_dump_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithOverriddenDump):
        return _echo_model(body)


# Enums


class _Color(Enum):
    red = "red"
    green = "green"


def _versioned_color_change() -> type[VersionChange]:
    return version_change(enum(_Color).had(blue="blue"))


class _StrictModelWithEnum(BaseModel):
    model_config = ConfigDict(strict=True)
    color: _Color


def _strict_response_model_with_enum(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_StrictModelWithEnum)
    async def route():
        return _StrictModelWithEnum(color=_Color.red)


class _ModelWithEnum(BaseModel):
    color: _Color


def _enum_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithEnum):
        return {"is_head_member": body.color is _Color.red}


def _enum_in_iterable_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Annotated[Iterable[_ModelWithEnum], Body()]):
        return [item.color is _Color.red for item in body]


# Validators


class _RequestWithNonIdempotentValidator(BaseModel):
    count: int

    @field_validator("count")
    @classmethod
    def increment(cls, value: int) -> int:
        _events.append("validator")
        return value + 1


def _non_idempotent_validator_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _RequestWithNonIdempotentValidator):
        return body.model_dump()


class _ResponseWithNonIdempotentValidator(BaseModel):
    count: int

    @field_validator("count")
    @classmethod
    def increment(cls, value: int) -> int:
        _events.append("validator")
        return value + 1


def _non_idempotent_validator_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ResponseWithNonIdempotentValidator)
    async def route():
        return _ResponseWithNonIdempotentValidator(count=1)


class _RequestWithBeforeModelValidator(BaseModel):
    new_name: str

    @model_validator(mode="before")
    @classmethod
    def rename(cls, data: Any) -> Any:
        return {"new_name": data["old_name"]}


def _before_model_validator_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _RequestWithBeforeModelValidator):
        return body.model_dump()


class _ModelWithInt(BaseModel):
    number: int


def _model_construct_with_wrong_type_in_response(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ModelWithInt)
    async def route():
        return _ModelWithInt.model_construct(number="1")


def _response_that_fails_validation(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ModelWithInt)
    async def route():
        return {"number": "not a number"}


# Serializers


class _ModelWithListSerializer(BaseModel):
    numbers: list[int]

    @field_serializer("numbers")
    def join(self, numbers: list[int]) -> str:
        return ",".join(map(str, numbers))


def _field_serializer_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithListSerializer):
        return {"numbers": body.numbers}


def _field_serializer_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ModelWithListSerializer)
    async def route():
        return _ModelWithListSerializer(numbers=[1, 2])


# Aliases


class _ModelWithValidationAlias(BaseModel):
    name: str = Field(validation_alias="in_name")
    other: int = 0


def _validation_alias_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithValidationAlias):
        return _echo_model(body)


class _ModelWithSerializationAlias(BaseModel):
    name: str = Field(serialization_alias="out_name")


def _serialization_alias_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithSerializationAlias):
        return body.model_dump()


def _serialization_alias_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ModelWithSerializationAlias)
    async def route():
        return _ModelWithSerializationAlias(name="a")


class _ModelWithDifferentAliases(BaseModel):
    name: str = Field(validation_alias="in_name", serialization_alias="out_name")


def _different_aliases_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithDifferentAliases):
        return body.model_dump()


def _different_aliases_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ModelWithDifferentAliases)
    async def route():
        return _ModelWithDifferentAliases.model_validate({"in_name": "a"})


class _ModelWithAliasChoices(BaseModel):
    name: str = Field(validation_alias=AliasChoices("first", "second"))


def _alias_choices_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithAliasChoices):
        return body.model_dump()


# Extra keys, set fields, containers, and unions


class _AllowingModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    count: int = 0


def _extra_allow_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _AllowingModel):
        return _echo_model(body)


def _list_of_models_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: list[_AllowingModel]):
        return [_echo_model(item) for item in body]


def _dict_of_models_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: dict[str, _AllowingModel]):
        return {key: _echo_model(item) for key, item in body.items()}


def _plain_dict_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Annotated[dict[str, Any], Body()]):
        return body


def _iterable_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Annotated[Iterable[_ModelWithInt], Body()]):
        return [item.number for item in body]


def _first_number(body: Annotated[Iterable[_ModelWithInt], Body()]) -> int:
    return next(iter(body)).number


def _iterable_in_request_body_read_by_dependency(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Annotated[Iterable[_ModelWithInt], Body()], first: Annotated[int, Depends(_first_number)]):
        return {"first": first, "all": [item.number for item in body]}


def _unset_fields_in_response_model_that_excludes_them(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_AllowingModel, response_model_exclude_unset=True)
    async def route():
        return _AllowingModel(name="a", extra_key=1)


def _union_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Union[_ModelWithInt, _AllowingModel]):
        return {"type": type(body).__name__, **_echo_model(body)}


def _union_with_serialization_alias_in_request_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Union[_ModelWithInt, _ModelWithSerializationAlias]):
        return {"type": type(body).__name__}


def _omitted_optional_body(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Union[_ModelWithInt, None] = None):
        return {"body": body}


# Forms and files


def _form_fields(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(name: Annotated[str, Form()], tags: Annotated[list[str], Form()]):
        return {"name": name, "tags": tags}


class _FormModel(BaseModel):
    name: str
    count: int = 0


def _form_model(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: Annotated[_FormModel, Form()]):
        return _echo_model(body)


def _uploaded_file(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(file: UploadFile):
        return {"name": file.filename, "content": (await file.read()).decode()}


# Dependencies and background tasks


def _record_call(request: Request) -> None:
    _events.append(f"dependency of {request.url.path}")


def _dependency_with_side_effect(router: APIRouter) -> None:
    @router.post(_PATH, dependencies=[Depends(_record_call)])
    async def route():
        _events.append("endpoint")
        return {}


def _add_background_task(background_tasks: BackgroundTasks) -> None:
    background_tasks.add_task(_events.append, "background task")


def _background_task_of_dependency_with_tasks_param(router: APIRouter) -> None:
    @router.post(_PATH, dependencies=[Depends(_add_background_task)])
    async def route(background_tasks: BackgroundTasks):
        return {"tasks": len(background_tasks.tasks)}


def _background_task_of_dependency_without_tasks_param(router: APIRouter) -> None:
    @router.post(_PATH, dependencies=[Depends(_add_background_task)])
    async def route():
        return {}


def _model_dependency(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(query: Annotated[_ModelWithInt, Depends()]):
        return query.model_dump()


# Plain params, errors, and responses


def _query_and_header_params(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(number: int, x_name: Annotated[Union[str, None], Header()] = None):
        return {"number": number, "name": x_name}


def _request_validation_error(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route(body: _ModelWithInt):  # pragma: no cover # FastAPI rejects the request
        return body


def _http_exception(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route():
        raise HTTPException(status_code=418, detail={"reason": "teapot"}, headers={"x-reason": "teapot"})


def _returned_json_response(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ModelWithInt)
    async def route():
        return JSONResponse({"number": "not validated"}, status_code=201)


def _streaming_response(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route():
        return StreamingResponse(iter(["a", "b"]), media_type="text/plain")


_CASES = [
    _Case("json_in_request_body", _json_in_request_body, request={"json": {"data": '"123"'}}),
    _Case("json_object_in_request_body", _json_object_in_request_body, request={"json": {"data": '{"a": 1}'}}),
    _Case(
        "json_in_request_body_with_type_field",
        _json_in_request_body_with_type_field,
        request={"json": {"data": '{"a": 1}'}},
    ),
    _Case(
        "json_in_request_body_named_metadata",
        _json_in_request_body_named_metadata,
        request={"json": {"metadata": '{"a": 1}'}},
    ),
    _Case("json_string_in_response_model", _json_string_in_response_model),
    _Case("json_object_in_response_model", _json_object_in_response_model),
    _Case("json_form_field", _json_form_field, request={"data": {"data": '{"a": 1}'}}),
    _Case("computed_field_in_response_model_that_forbids_extra", _computed_field_in_response_model_that_forbids_extra),
    _Case(
        "computed_field_in_request_body_that_forbids_extra",
        _computed_field_in_request_body_that_forbids_extra,
        request={"json": {"name": "a"}},
    ),
    _Case(
        "overridden_model_dump_in_request_body",
        _overridden_model_dump_in_request_body,
        request={"json": {"name": "a"}},
    ),
    _Case("strict_response_model_with_enum", _strict_response_model_with_enum),
    _Case(
        "strict_response_model_with_versioned_enum",
        _strict_response_model_with_enum,
        change=_versioned_color_change,
        untouched_versions=(_NEWER_VERSION,),
    ),
    _Case("enum_in_request_body", _enum_in_request_body, request={"json": {"color": "red"}}),
    _Case(
        "enum_in_request_body_with_another_versioned_enum",
        _enum_in_request_body,
        request={"json": {"color": "red"}},
        change=lambda: version_change(enum(_UnrelatedEnum).had(b="b")),
    ),
    _Case(
        "versioned_enum_in_iterable_request_body",
        _enum_in_iterable_request_body,
        request={"json": [{"color": "red"}]},
        change=_versioned_color_change,
        untouched_versions=(_NEWER_VERSION,),
    ),
    _Case(
        "non_idempotent_validator_in_request_body",
        _non_idempotent_validator_in_request_body,
        request={"json": {"count": 1}},
    ),
    _Case("non_idempotent_validator_in_response_model", _non_idempotent_validator_in_response_model),
    _Case(
        "before_model_validator_in_request_body",
        _before_model_validator_in_request_body,
        request={"json": {"old_name": "a"}},
    ),
    _Case("model_construct_with_wrong_type_in_response", _model_construct_with_wrong_type_in_response),
    _Case("response_that_fails_validation", _response_that_fails_validation),
    _Case("field_serializer_in_request_body", _field_serializer_in_request_body, request={"json": {"numbers": [1, 2]}}),
    _Case("field_serializer_in_response_model", _field_serializer_in_response_model),
    _Case("validation_alias_in_request_body", _validation_alias_in_request_body, request={"json": {"in_name": "a"}}),
    _Case("serialization_alias_in_request_body", _serialization_alias_in_request_body, request={"json": {"name": "a"}}),
    _Case("serialization_alias_in_response_model", _serialization_alias_in_response_model),
    _Case("different_aliases_in_request_body", _different_aliases_in_request_body, request={"json": {"in_name": "a"}}),
    _Case("different_aliases_in_response_model", _different_aliases_in_response_model),
    _Case("alias_choices_in_request_body", _alias_choices_in_request_body, request={"json": {"second": "a"}}),
    _Case("extra_allow_in_request_body", _extra_allow_in_request_body, request={"json": {"name": "a", "extra": 1}}),
    _Case(
        "list_of_models_in_request_body",
        _list_of_models_in_request_body,
        request={"json": [{"name": "a", "extra": 1}]},
    ),
    _Case(
        "list_of_models_in_request_body_with_dependency_overrides",
        _list_of_models_in_request_body,
        request={"json": [{"name": "a", "extra": 1}]},
        dependency_overrides={_add_background_task: _add_background_task},
    ),
    _Case(
        "dict_of_models_in_request_body",
        _dict_of_models_in_request_body,
        request={"json": {"key": {"name": "a", "extra": 1}}},
    ),
    _Case("plain_dict_in_request_body", _plain_dict_in_request_body, request={"json": {"a": [1, {"b": None}]}}),
    _Case("iterable_in_request_body", _iterable_in_request_body, request={"json": [{"number": 1}, {"number": 2}]}),
    _Case(
        "iterable_in_request_body_read_by_dependency",
        _iterable_in_request_body_read_by_dependency,
        request={"json": [{"number": 1}, {"number": 2}]},
    ),
    _Case("unset_fields_in_response_model_that_excludes_them", _unset_fields_in_response_model_that_excludes_them),
    _Case("union_in_request_body", _union_in_request_body, request={"json": {"name": "a", "extra": 1}}),
    _Case(
        "union_with_serialization_alias_in_request_body",
        _union_with_serialization_alias_in_request_body,
        request={"json": {"name": "a"}},
    ),
    _Case("omitted_optional_body", _omitted_optional_body),
    _Case("form_fields", _form_fields, request={"data": {"name": "a", "tags": ["b", "c"]}}),
    _Case("form_model", _form_model, request={"data": {"name": "a"}}),
    _Case("uploaded_file", _uploaded_file, request={"files": {"file": ("a.txt", b"content")}}),
    _Case("dependency_with_side_effect", _dependency_with_side_effect),
    _Case("background_task_of_dependency_with_tasks_param", _background_task_of_dependency_with_tasks_param),
    _Case("background_task_of_dependency_without_tasks_param", _background_task_of_dependency_without_tasks_param),
    _Case("model_dependency", _model_dependency, request={"params": {"number": "1"}}),
    _Case(
        "query_and_header_params",
        _query_and_header_params,
        request={"params": {"number": "1"}, "headers": {"x-name": "a"}},
    ),
    _Case("request_validation_error", _request_validation_error, request={"json": {"number": "a"}}),
    _Case("http_exception", _http_exception),
    _Case("returned_json_response", _returned_json_response),
    _Case("streaming_response", _streaming_response),
]


# FastAPI serializes the wrong-typed values of `model_construct()` objects with a warning
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
@pytest.mark.parametrize("case", _CASES, ids=[case.id for case in _CASES])
def test__untouched_route__request__should_get_the_same_response_as_in_fastapi(case: _Case):
    fastapi_app, app = _create_apps(case)
    assert _get_untouched_versions(app) == case.untouched_versions

    expected = _send(TestClient(fastapi_app, raise_server_exceptions=False), case, {})
    client = TestClient(app, raise_server_exceptions=False)
    results = {
        version: _send(client, case, {app.router.api_version_parameter_name: version})
        for version in case.untouched_versions
    }

    if case.id in _FASTAPI_DEVIATIONS:
        assert results != dict.fromkeys(case.untouched_versions, expected), (
            f'"{case.id}" behaves like FastAPI now. Remove it from _FASTAPI_DEVIATIONS'
        )
    else:
        assert results == dict.fromkeys(case.untouched_versions, expected)


def test__fastapi_deviations__every_entry__should_name_a_case():
    assert set(_FASTAPI_DEVIATIONS) <= {case.id for case in _CASES}


# Routes that version changes touch


def _request_migration_by_path() -> type[VersionChange]:
    @convert_request_to_next_version_for(_PATH, ["POST"])
    def migration(request: RequestInfo) -> None:  # pragma: no cover
        pass

    return version_change(migration=migration)


def _response_migration_by_path() -> type[VersionChange]:
    @convert_response_to_previous_version_for(_PATH, ["POST"])
    def migration(response: ResponseInfo) -> None:  # pragma: no cover
        pass

    return version_change(migration=migration)


def _request_migration_by_schema() -> type[VersionChange]:
    @convert_request_to_next_version_for(_ModelWithValidationAlias)
    def migration(request: RequestInfo) -> None:  # pragma: no cover
        pass

    return version_change(migration=migration)


def _response_migration_by_schema() -> type[VersionChange]:
    @convert_response_to_previous_version_for(_ModelWithSerializationAlias)
    def migration(response: ResponseInfo) -> None:  # pragma: no cover
        pass

    return version_change(migration=migration)


def _request_migration_of_union_member() -> type[VersionChange]:
    @convert_request_to_next_version_for(_AllowingModel)
    def migration(request: RequestInfo) -> None:  # pragma: no cover
        pass

    return version_change(migration=migration)


def _response_migration_of_union_member() -> type[VersionChange]:
    @convert_response_to_previous_version_for(_ModelWithInt)
    def migration(response: ResponseInfo) -> None:  # pragma: no cover
        pass

    return version_change(migration=migration)


class _ResponseModelWithNestedModel(BaseModel):
    nested: _ModelWithInt


def _union_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=Union[_ModelWithInt, _ModelWithSerializationAlias])
    async def route():  # pragma: no cover
        return _ModelWithInt(number=1)


def _nested_model_in_response_model(router: APIRouter) -> None:
    @router.post(_PATH, response_model=_ResponseModelWithNestedModel)
    async def route():  # pragma: no cover
        return _ResponseModelWithNestedModel(nested=_ModelWithInt(number=1))


def _dependency_that_asks_for_the_solver(router: APIRouter) -> None:
    @router.post(_PATH, dependencies=[Depends(current_dependency_solver)])
    async def route():  # pragma: no cover
        return {}


def _dependency_with_names_only_for_type_checkers(
    # FastAPI reads it as a query param because it can't resolve the name, so we can't tell what Cadwyn versions
    value: "Annotated[_OnlyForTypeCheckers, Depends(_record_call)]",
) -> None:  # pragma: no cover
    pass


def _route_with_names_only_for_type_checkers(router: APIRouter) -> None:
    @router.post(_PATH, dependencies=[Depends(_dependency_with_names_only_for_type_checkers)])
    async def route():  # pragma: no cover
        return {}


def _route_that_only_exists_in_older_versions(router: APIRouter) -> None:
    @router.post(_PATH)
    async def route():  # pragma: no cover
        return {}

    if isinstance(router, VersionedAPIRouter):
        router.only_exists_in_older_versions(route)


@pytest.mark.parametrize(
    ("register", "change", "head_changes", "untouched_versions"),
    [
        pytest.param(
            _dependency_with_side_effect, _request_migration_by_path, (), (_NEWER_VERSION,), id="request_path"
        ),
        pytest.param(
            _dependency_with_side_effect, _response_migration_by_path, (), (_NEWER_VERSION,), id="response_path"
        ),
        pytest.param(
            _validation_alias_in_request_body, _request_migration_by_schema, (), (_NEWER_VERSION,), id="request_schema"
        ),
        pytest.param(
            _serialization_alias_in_response_model,
            _response_migration_by_schema,
            (),
            (_NEWER_VERSION,),
            id="response_schema",
        ),
        pytest.param(
            _union_in_request_body, _request_migration_of_union_member, (), (_NEWER_VERSION,), id="request_union_member"
        ),
        pytest.param(
            _union_in_response_model,
            _response_migration_of_union_member,
            (),
            (_NEWER_VERSION,),
            id="response_union_member",
        ),
        pytest.param(
            _validation_alias_in_request_body,
            lambda: version_change(schema(_ModelWithValidationAlias).field("other").didnt_exist),
            (),
            (_NEWER_VERSION,),
            id="changed_request_body",
        ),
        pytest.param(
            _nested_model_in_response_model,
            lambda: version_change(schema(_ModelWithInt).field("number").had(description="A number")),
            (),
            (_NEWER_VERSION,),
            id="changed_model_in_response_model",
        ),
        pytest.param(
            _model_dependency,
            lambda: version_change(schema(_ModelWithInt).field("number").had(description="A number")),
            (),
            (_NEWER_VERSION,),
            id="changed_model_of_dependency",
        ),
        pytest.param(
            _enum_in_request_body, _versioned_color_change, (), (_NEWER_VERSION,), id="changed_enum_in_request_body"
        ),
        pytest.param(
            _validation_alias_in_request_body,
            _unrelated_change,
            (version_change(schema(_ModelWithValidationAlias).field("other").didnt_exist),),
            (),
            id="changed_by_head_version",
        ),
        pytest.param(
            _dependency_with_side_effect,
            lambda: version_change(endpoint(_PATH, ["POST"]).had(description="Old")),
            (),
            (_NEWER_VERSION,),
            id="endpoint_had",
        ),
        pytest.param(
            _dependency_with_side_effect,
            lambda: version_change(endpoint(_PATH, ["POST"]).didnt_exist),
            (),
            (_NEWER_VERSION,),
            id="endpoint_didnt_exist",
        ),
        pytest.param(
            _route_that_only_exists_in_older_versions,
            lambda: version_change(endpoint(_PATH, ["POST"]).existed),
            (),
            (),
            id="endpoint_existed",
        ),
        pytest.param(_dependency_that_asks_for_the_solver, _unrelated_change, (), (), id="current_dependency_solver"),
        pytest.param(
            _route_with_names_only_for_type_checkers, _unrelated_change, (), (), id="unknown_models_of_dependency"
        ),
    ],
)
def test__route_generation__version_changes_touch_route__should_record_it_as_touched_in_their_older_versions(
    register: Callable[[APIRouter], None],
    change: Callable[[], type[VersionChange]],
    head_changes: tuple[type[VersionChange], ...],
    untouched_versions: tuple[str, ...],
):
    _, app = _create_apps(_Case("touched", register, change=change), *head_changes)

    assert _get_untouched_versions(app) == untouched_versions
