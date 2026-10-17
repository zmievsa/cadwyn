"""Dependency building blocks whose postponed annotations can only be resolved in this module's globals"""

from __future__ import annotations

import types
from typing import Any, Optional

from pydantic import BaseModel


class Payload(BaseModel):
    foo: str


def initialize_with_label(self: Any, body: Payload, label: str) -> None:
    self.body = body
    self.label = label


def initialize(self: Any, body: Payload) -> None:
    self.body = body


def create(cls: type, body: Payload) -> Any:
    resource: Any = object.__new__(cls)
    resource.body = body
    return resource


def metaclass_call(mcs: type, body: Payload) -> Any:
    return types.SimpleNamespace(body=body)


def get_body_with_label(body: Payload, label: str) -> Payload:
    return body


def get_body(body: Payload) -> Payload:
    return body


def get_body_with_optional_quoted_body(body: Payload, other: Optional["Payload"] = None) -> Payload:  # noqa: UP037
    return body


def provide(self: Any, body: Payload) -> Payload:
    return body


def provide_with_label(self: Any, body: Payload, label: str) -> Payload:
    return body


def provide_with_class(cls: type, body: Payload) -> Payload:
    return body


class Resource:
    def __init__(self, body: Payload) -> None:
        self.body = body


class CallableProvider:
    def __call__(self, body: Payload) -> Payload:
        return body


callable_provider = CallableProvider()


class Provider:
    def provide(self, body: Payload) -> Payload:
        return body


provider = Provider()
