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

    def __post_init__(self):
        functools.update_wrapper(self, self.transformer)

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

        super().__post_init__()

    def __call__(self, data: Any) -> None:
        return self.transformer(self.owner, data)


@dataclass
class AlterRequestInstruction(_AlterDataInstruction):
    path: str
    methods: Sequence[str]

    def __post_init__(self):
        signature = inspect.signature(self.transformer)
        if list(signature.parameters) != ["cls", "request"]:
            raise ValueError(
                f"Method '{self.transformer.__name__}' must have 2 parameters: cls and request",
            )

        super().__post_init__()

    # TODO: This might be unnecessary. Check, please
    def __call__(self, request: Any) -> None:
        return self.transformer(self.owner, request)


def convert_response_to_previous_version_for(schema: Any, /) -> "type[classmethod[Any, _P, None]]":
    def decorator(transformer: Callable[[object, Any], None]) -> Any:
        return AlterResponseInstruction(schema=schema, transformer=transformer)

    return decorator  # pyright: ignore[reportGeneralTypeIssues]


def convert_request_to_next_version_for(path: Any, /, methods: list[str]) -> "type[classmethod[Any, _P, None]]":
    def decorator(transformer: Callable[[object, Any], None]) -> Any:
        return AlterRequestInstruction(transformer=transformer, path=path, methods=methods)

    return decorator  # pyright: ignore[reportGeneralTypeIssues]
