import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

from .common import Endpoint

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass
class AlterResponseInstruction:
    endpoints: tuple[Endpoint, ...]
    method: Callable[[type, dict[str, Any]], None]
    owner: type = field(init=False)

    def __post_init__(self):
        assert inspect.signature(self.method).return_annotation is None
        assert (
            len(inspect.signature(self.method).parameters) == 2
        ), f"Method {self.method.__name__} must have 2 parameters: cls and data"
        annotation = inspect.signature(self.method).parameters["data"].annotation
        assert annotation == dict[str, Any], annotation

        functools.update_wrapper(self, self.method)

    def __set_name__(self, owner, name):
        self.owner = owner

    def __call__(self, data: dict[str, Any]) -> None:
        return self.method(self.owner, data)


def convert_response_to_previous_version_for(
    first_endpoint: Endpoint, /, *other_endpoints: Endpoint,
) -> "type[classmethod[Any, _P, None]]":
    def decorator(method: Callable[[object, dict[str, Any]], None]) -> Any:
        return AlterResponseInstruction((first_endpoint, *other_endpoints), method)

    return decorator  # pyright: ignore[reportGeneralTypeIssues]
