from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import Response
from fastapi.params import Body, Depends, Param
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from .._utils import Sentinel
from .common import Endpoint


@dataclass(slots=True)
class EndpointDidntHaveDependencyInstruction:
    endpoint: Endpoint
    dependency_name: str


@dataclass(slots=True)
class EndpointHadDependencyInstruction:
    endpoint: Endpoint
    dependency_name: str
    dependency: Depends | Body | Param


@dataclass(slots=True)
class EndpointAttributesPayload:
    # Fastapi API routes also have "endpoint" and "dependency_overrides_provider" fields.
    # We do not use them because:
    #   1. "endpoint" must not change -- otherwise this versioning is doomed
    #   2. "dependency_overrides_provider" is taken from router's attributes
    #   3. "response_model" must not change for the same reason as endpoint
    # The following for the same reason as enpoint:
    # * response_model_include: SetIntStr | DictIntStrAny
    # * response_model_exclude: SetIntStr | DictIntStrAny
    # * response_model_by_alias: bool
    # * response_model_exclude_unset: bool
    # * response_model_exclude_defaults: bool
    # * response_model_exclude_none: bool
    path: str
    status_code: int
    tags: list[str | Enum]
    dependencies: Sequence[Depends]
    summary: str
    description: str
    response_description: str
    responses: dict[int | str, dict[str, Any]]
    deprecated: bool
    methods: list[str]
    operation_id: str
    include_in_schema: bool
    response_class: type[Response]
    name: str
    callbacks: list[BaseRoute]
    openapi_extra: dict[str, Any]
    generate_unique_id_function: Callable[[APIRoute], str]


@dataclass(slots=True)
class EndpointHadInstruction:
    endpoint: Endpoint
    attributes: EndpointAttributesPayload


@dataclass(slots=True)
class EndpointExistedInstruction:
    endpoint: Endpoint


@dataclass(slots=True)
class EndpointDidntExistInstruction:
    endpoint: Endpoint


@dataclass(slots=True)
class EndpointInstructionFactory:
    endpoint: Endpoint

    @property
    def didnt_exist(self) -> EndpointDidntExistInstruction:
        return EndpointDidntExistInstruction(self.endpoint)

    @property
    def existed(self) -> EndpointExistedInstruction:
        return EndpointExistedInstruction(self.endpoint)

    def had(
        self,
        path: str = Sentinel,
        status_code: int = Sentinel,
        tags: list[str | Enum] = Sentinel,
        dependencies: Sequence[Depends] = Sentinel,
        summary: str = Sentinel,
        description: str = Sentinel,
        response_description: str = Sentinel,
        responses: dict[int | str, dict[str, Any]] = Sentinel,
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
            self.endpoint,
            EndpointAttributesPayload(
                path=path,
                status_code=status_code,
                tags=tags,
                dependencies=dependencies,
                summary=summary,
                description=description,
                response_description=response_description,
                responses=responses,
                deprecated=deprecated,
                methods=methods,
                operation_id=operation_id,
                include_in_schema=include_in_schema,
                response_class=response_class,
                name=name,
                callbacks=callbacks,
                openapi_extra=openapi_extra,
                generate_unique_id_function=generate_unique_id_function,
            ),
        )


# Adding/removing  dependencies between versions seems like a bad choice.
# It makes the system overly complex.

# def didnt_have_dependency(
#     self,
#     name: str,
# ) -> EndpointDidntHaveDependencyInstruction:
#     return EndpointDidntHaveDependencyInstruction(self.endpoint, name)

# def had_dependency(
#     self,
#     name: str,
#     dependency: Depends | Body | Param,
# ) -> EndpointHadDependencyInstruction:
#     return EndpointHadDependencyInstruction(self.endpoint, name, dependency)


def endpoint(endpoint: Endpoint, /) -> EndpointInstructionFactory:
    return EndpointInstructionFactory(endpoint)


AlterEndpointSubInstruction = (
    EndpointDidntExistInstruction
    | EndpointExistedInstruction
    | EndpointHadInstruction
    # | EndpointDidntHaveDependencyInstruction
    # | EndpointHadDependencyInstruction
)
