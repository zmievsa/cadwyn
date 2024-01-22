import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, ParamSpec, cast, overload

from fastapi import Request, Response
from starlette.datastructures import MutableHeaders

from cadwyn._utils import same_definition_as_in

_P = ParamSpec("_P")


# TODO (https://github.com/zmievsa/cadwyn/issues/49): Add form handling
class RequestInfo:
    __slots__ = ("body", "headers", "_cookies", "_query_params", "_request")

    def __init__(self, request: Request, body: Any):
        super().__init__()
        self.body = body
        self.headers = request.headers.mutablecopy()
        self._cookies = request.cookies
        self._query_params = request.query_params._dict
        self._request = request

    @property
    def cookies(self) -> dict[str, str]:
        return self._cookies

    @property
    def query_params(self) -> dict[str, str]:
        return self._query_params


# TODO (https://github.com/zmievsa/cadwyn/issues/111): handle _response.media_type and _response.background
class ResponseInfo:
    __slots__ = ("body", "_response")

    def __init__(self, response: Response, body: Any):
        super().__init__()
        self.body = body
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @status_code.setter
    def status_code(self, value: int):
        self._response.status_code = value

    @property
    def headers(self) -> MutableHeaders:
        return self._response.headers

    @same_definition_as_in(Response.set_cookie)
    def set_cookie(self, *args: Any, **kwargs: Any):
        return self._response.set_cookie(*args, **kwargs)

    @same_definition_as_in(Response.delete_cookie)
    def delete_cookie(self, *args: Any, **kwargs: Any):
        return self._response.delete_cookie(*args, **kwargs)


@dataclass
class _AlterDataInstruction:
    transformer: Callable[[Any], None]
    owner: type = field(init=False)
    _payload_arg_name: ClassVar[str]

    def __post_init__(self):
        signature = inspect.signature(self.transformer)
        if list(signature.parameters) != [self._payload_arg_name]:
            raise ValueError(
                f"Method '{self.transformer.__name__}' must have only 1 parameter: {self._payload_arg_name}",
            )

        functools.update_wrapper(self, self.transformer)

    def __set_name__(self, owner: type, name: str):
        self.owner = owner

    def __call__(self, __request_or_response: RequestInfo | ResponseInfo, /) -> None:
        return self.transformer(__request_or_response)


###########
## Requests
###########


@dataclass
class _BaseAlterRequestInstruction(_AlterDataInstruction):
    _payload_arg_name = "request"


@dataclass
class AlterRequestBySchemaInstruction(_BaseAlterRequestInstruction):
    schema: Any


@dataclass
class AlterRequestByPathInstruction(_BaseAlterRequestInstruction):
    path: str
    methods: set[str]


@overload
def convert_request_to_next_version_for(schema: type, /) -> "type[staticmethod[_P, None]]":
    ...


@overload
def convert_request_to_next_version_for(path: str, methods: list[str], /) -> "type[staticmethod[_P, None]]":
    ...


def convert_request_to_next_version_for(
    schema_or_path: type | str,
    methods: list[str] | None = None,
    /,
) -> "type[staticmethod[_P, None]]":
    _validate_decorator_args(schema_or_path, methods)

    def decorator(transformer: Callable[[RequestInfo], None]) -> Any:
        if isinstance(schema_or_path, str):
            return AlterRequestByPathInstruction(
                path=schema_or_path,
                methods=set(cast(list, methods)),
                transformer=transformer,
            )
        else:
            return AlterRequestBySchemaInstruction(
                schema=schema_or_path,
                transformer=transformer,
            )

    return decorator  # pyright: ignore[reportGeneralTypeIssues]


############
## Responses
############


class _BaseAlterResponseInstruction(_AlterDataInstruction):
    _payload_arg_name = "response"


@dataclass
class AlterResponseBySchemaInstruction(_BaseAlterResponseInstruction):
    schema: Any


@dataclass
class AlterResponseByPathInstruction(_BaseAlterResponseInstruction):
    path: str
    methods: set[str]


@overload
def convert_response_to_previous_version_for(schema: type, /) -> "type[staticmethod[_P, None]]":
    ...


@overload
def convert_response_to_previous_version_for(path: str, methods: list[str], /) -> "type[staticmethod[_P, None]]":
    ...


def convert_response_to_previous_version_for(
    schema_or_path: type | str,
    methods: list[str] | None = None,
    /,
) -> "type[staticmethod[_P, None]]":
    _validate_decorator_args(schema_or_path, methods)

    def decorator(transformer: Callable[[ResponseInfo], None]) -> Any:
        if isinstance(schema_or_path, str):
            # The validation above checks that methods is not None
            return AlterResponseByPathInstruction(
                path=schema_or_path,
                methods=set(cast(list, methods)),
                transformer=transformer,
            )
        else:
            return AlterResponseBySchemaInstruction(schema=schema_or_path, transformer=transformer)

    return decorator  # pyright: ignore[reportGeneralTypeIssues]


def _validate_decorator_args(schema_or_path: type | str, methods: list[str] | None):
    if isinstance(schema_or_path, str):
        if methods is None:
            raise ValueError("If path was provided as a first argument, methods must be provided as a second argument")

    elif methods is not None:
        raise ValueError("If schema was provided as a first argument, methods argument should not be provided")
