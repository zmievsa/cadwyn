import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ParamSpec, Sequence

_P = ParamSpec("_P")


@dataclass
class _AlterDataInstruction:
    transformer: Callable[[type, Any], None]
    owner: type = field(init=False)

    def __set_name__(self, owner: type, name: str):
        self.owner = owner


@dataclass
class AlterResponseInstruction(_AlterDataInstruction):
    schema: Any

    def __post_init__(self):
        signature = inspect.signature(self.transformer)
        if list(signature.parameters) != ["cls", "data"]:
            raise ValueError(
                f"Method '{self.transformer.__name__}' must have 2 parameters: cls and data",
            )

        functools.update_wrapper(self, self.transformer)

    def __call__(self, data: Any) -> None:
        return self.transformer(self.owner, data)


@dataclass
class AlterRequestInstruction(_AlterDataInstruction):
    path: str
    methods: Sequence[str]


def convert_response_to_previous_version_for(schema: Any, /) -> "type[classmethod[Any, _P, None]]":
    def decorator(transformer: Callable[[object, Any], None]) -> Any:
        return AlterResponseInstruction(schema=schema, transformer=transformer)

    return decorator  # pyright: ignore[reportGeneralTypeIssues]
