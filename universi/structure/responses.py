import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel

_P = ParamSpec("_P")


@dataclass
class AlterResponseInstruction:
    schema: Any
    method: Callable[[type, Any], None]
    owner: type = field(init=False)

    def __post_init__(self):
        signature = inspect.signature(self.method)
        if list(signature.parameters) != ["cls", "data"]:
            raise ValueError(
                f"Method '{self.method.__name__}' must have 2 parameters: cls and data",
            )

        functools.update_wrapper(self, self.method)

    def __set_name__(self, owner: type, name: str):
        self.owner = owner

    def __call__(self, data: Any) -> None:
        return self.method(self.owner, data)


# TODO: Change it to Any
def convert_response_to_previous_version_for(schema: type[BaseModel], /) -> "type[classmethod[Any, _P, None]]":
    def decorator(method: Callable[[object, Any], None]) -> Any:
        return AlterResponseInstruction(schema, method)

    return decorator  # pyright: ignore[reportGeneralTypeIssues]
