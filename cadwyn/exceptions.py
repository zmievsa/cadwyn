from fastapi.routing import APIRoute


class CadwynError(Exception):
    pass


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
