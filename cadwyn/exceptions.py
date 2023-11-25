from fastapi.routing import APIRoute
from verselect.exceptions import AppCreationError

__all__ = [
    "AppCreationError",
    "CadwynError",
    "LintingError",
    "CodeGenerationError",
    "InvalidGenerationInstructionError",
    "RouterGenerationError",
    "RouteAlreadyExistsError",
    "CadwynStructureError",
    "ModuleIsNotVersionedError",
]


class CadwynError(Exception):
    pass


class LintingError(CadwynError):
    pass


class CodeGenerationError(CadwynError):
    pass


class InvalidGenerationInstructionError(CodeGenerationError):
    pass


class RouterGenerationError(CadwynError):
    pass


class RouteAlreadyExistsError(RouterGenerationError):
    def __init__(self, *routes: APIRoute):
        self.routes = routes
        super().__init__(f"The following routes are duplicates of each other: {routes}")


class CadwynStructureError(CadwynError):
    pass


class ModuleIsNotVersionedError(ValueError):
    pass
