"""The errors of Cadwyn, which has the errors of cadwyn-core too, as these are the same classes."""

from typing import TYPE_CHECKING, Union

from cadwyn_core.exceptions import (
    CadwynError,
    CadwynHeadRequestValidationError,
    CadwynRenderError,
    CadwynStructureError,
    InvalidGenerationInstructionError,
    LintingError,
    SchemaGenerationError,
)

if TYPE_CHECKING:
    from fastapi.routing import APIRoute, APIWebSocketRoute


class ModuleIsNotAvailableAsTextError(SchemaGenerationError):
    pass


class RouterGenerationError(CadwynError):
    pass


class RouterPathParamsModifiedError(RouterGenerationError):
    pass


class RouteResponseBySchemaConverterDoesNotApplyToAnythingError(RouterGenerationError):
    pass


class RouteRequestBySchemaConverterDoesNotApplyToAnythingError(RouterGenerationError):
    pass


class RouteByPathConverterDoesNotApplyToAnythingError(RouterGenerationError):
    pass


class RouteAlreadyExistsError(RouterGenerationError):
    def __init__(self, *routes: "Union[APIRoute, APIWebSocketRoute]") -> None:
        self.routes = routes
        super().__init__(f"The following routes are duplicates of each other: {routes}")


class ModuleIsNotVersionedError(ValueError):
    pass


class ImportFromStringError(CadwynError):
    pass


__all__ = [
    "CadwynError",
    "CadwynHeadRequestValidationError",
    "CadwynRenderError",
    "CadwynStructureError",
    "ImportFromStringError",
    "InvalidGenerationInstructionError",
    "LintingError",
    "ModuleIsNotAvailableAsTextError",
    "ModuleIsNotVersionedError",
    "RouteAlreadyExistsError",
    "RouteByPathConverterDoesNotApplyToAnythingError",
    "RouteRequestBySchemaConverterDoesNotApplyToAnythingError",
    "RouteResponseBySchemaConverterDoesNotApplyToAnythingError",
    "RouterGenerationError",
    "RouterPathParamsModifiedError",
    "SchemaGenerationError",
]
