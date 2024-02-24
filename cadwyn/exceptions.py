import json
from datetime import date
from typing import Any

from fastapi.routing import APIRoute


class CadwynError(Exception):
    pass


class CadwynLatestRequestValidationError(CadwynError):
    def __init__(self, errors: list[Any], body: Any, version: date) -> None:
        self.errors = errors
        self.body = body
        self.version = version
        super().__init__(
            f"We failed to migrate the request with version={self.version!s}. "
            "This means that there is some error in your migrations or schema structure that makes it impossible "
            "to migrate the request of that version to latest.\n"
            f"body={self.body}\n\nerrors={json.dumps(self.errors, indent=4, ensure_ascii=False)}"
        )


class LintingError(CadwynError):
    pass


class CodeGenerationError(CadwynError):
    pass


class ModuleIsNotAvailableAsTextError(CodeGenerationError):
    pass


class InvalidGenerationInstructionError(CodeGenerationError):
    pass


class RouterGenerationError(CadwynError):
    pass


class RouterPathParamsModifiedError(RouterGenerationError):
    pass


class RouteAlreadyExistsError(RouterGenerationError):
    def __init__(self, *routes: APIRoute):
        self.routes = routes
        super().__init__(f"The following routes are duplicates of each other: {routes}")


class CadwynStructureError(CadwynError):
    pass


class ModuleIsNotVersionedError(ValueError):
    pass
