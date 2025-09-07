from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union

from fastapi import Response
from fastapi.params import Depends
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from cadwyn.exceptions import LintingError

from .._utils import DATACLASS_SLOTS, Sentinel
from .common import _HiddenAttributeMixin

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


@dataclass(**DATACLASS_SLOTS)
class EndpointAttributesPayload:
    # FastAPI API routes also have "endpoint" and "dependency_overrides_provider" fields.
    # We do not use them because:
    #   1. "endpoint" must not change -- otherwise this versioning is doomed
    #   2. "dependency_overrides_provider" is taken from router's attributes
    #   3. "response_model" must not change for the same reason as endpoint
    # The following for the same reason as endpoint:
    # * response_model_include: SetIntStr | DictIntStrAny
    # * response_model_exclude: SetIntStr | DictIntStrAny
    # * response_model_by_alias: bool
    # * response_model_exclude_unset: bool
    # * response_model_exclude_defaults: bool
    # * response_model_exclude_none: bool
    path: str
    response_model: Any
    status_code: int
    tags: list[Union[str, Enum]]
    # Adding/removing dependencies between versions seems like a bad choice.
    # It makes the system overly complex. Instead, we allow people to
    # overwrite all dependencies of a route at once. Hence you always know exactly
    # which dependencies have been specified, no matter how many migrations you have.

    dependencies: Sequence[Depends]
    summary: str
    description: str
    response_description: str
    responses: dict[Union[int, str], dict[str, Any]]
    deprecated: bool
    methods: set[str]
    operation_id: str
    include_in_schema: bool
    response_class: type[Response]
    name: str
    callbacks: list[BaseRoute]
    openapi_extra: dict[str, Any]
    generate_unique_id_function: Callable[[APIRoute], str]


@dataclass(**DATACLASS_SLOTS)
class EndpointHadInstruction(_HiddenAttributeMixin):
    endpoint_path: str
    endpoint_methods: set[str]
    endpoint_func_name: Union[str, None]
    attributes: EndpointAttributesPayload


@dataclass(**DATACLASS_SLOTS)
class EndpointExistedInstruction(_HiddenAttributeMixin):
    endpoint_path: str
    endpoint_methods: set[str]
    endpoint_func_name: Union[str, None]


@dataclass(**DATACLASS_SLOTS)
class EndpointDidntExistInstruction(_HiddenAttributeMixin):
    endpoint_path: str
    endpoint_methods: set[str]
    endpoint_func_name: Union[str, None]


@dataclass(**DATACLASS_SLOTS)
class EndpointInstructionFactory:
    endpoint_path: str
    endpoint_methods: set[str]
    endpoint_func_name: Union[str, None]

    @property
    def didnt_exist(self) -> EndpointDidntExistInstruction:
        return EndpointDidntExistInstruction(
            is_hidden_from_changelog=False,
            endpoint_path=self.endpoint_path,
            endpoint_methods=self.endpoint_methods,
            endpoint_func_name=self.endpoint_func_name,
        )

    @property
    def existed(self) -> EndpointExistedInstruction:
        return EndpointExistedInstruction(
            is_hidden_from_changelog=False,
            endpoint_path=self.endpoint_path,
            endpoint_methods=self.endpoint_methods,
            endpoint_func_name=self.endpoint_func_name,
        )

    def had(
        self,
        *,
        path: str = Sentinel,
        response_model: Any = Sentinel,
        status_code: int = Sentinel,
        tags: list[Union[str, Enum]] = Sentinel,
        dependencies: Sequence[Depends] = Sentinel,
        summary: str = Sentinel,
        description: str = Sentinel,
        response_description: str = Sentinel,
        responses: dict[Union[int, str], dict[str, Any]] = Sentinel,
        deprecated: bool = Sentinel,
        methods: list[str] = Sentinel,
        operation_id: str = Sentinel,
        include_in_schema: bool = Sentinel,
        response_class: type[Response] = Sentinel,
        name: str = Sentinel,
        callbacks: list[BaseRoute] = Sentinel,
        openapi_extra: dict[str, Any] = Sentinel,
        generate_unique_id_function: Callable[[APIRoute], str] = Sentinel,
    ):
        return EndpointHadInstruction(
            is_hidden_from_changelog=False,
            endpoint_path=self.endpoint_path,
            endpoint_methods=self.endpoint_methods,
            endpoint_func_name=self.endpoint_func_name,
            attributes=EndpointAttributesPayload(
                path=path,
                response_model=response_model,
                status_code=status_code,
                tags=tags,
                dependencies=dependencies,
                summary=summary,
                description=description,
                response_description=response_description,
                responses=responses,
                deprecated=deprecated,
                methods=set(methods) if methods is not Sentinel else Sentinel,
                operation_id=operation_id,
                include_in_schema=include_in_schema,
                response_class=response_class,
                name=name,
                callbacks=callbacks,
                openapi_extra=openapi_extra,
                generate_unique_id_function=generate_unique_id_function,
            ),
        )


def endpoint(path: str, methods: list[str], /, *, func_name: Union[str, None] = None) -> EndpointInstructionFactory:
    _validate_that_strings_are_valid_http_methods(methods)

    return EndpointInstructionFactory(path, set(methods), func_name)


def _validate_that_strings_are_valid_http_methods(methods: Collection[str]):
    invalid_methods = set(methods) - HTTP_METHODS
    if invalid_methods:
        invalid_methods = ", ".join(sorted(invalid_methods))
        raise LintingError(
            f"The following HTTP methods are not valid: {invalid_methods}. "
            "Please use valid HTTP methods such as GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD.",
        )


AlterEndpointSubInstruction = Union[EndpointDidntExistInstruction, EndpointExistedInstruction, EndpointHadInstruction]
