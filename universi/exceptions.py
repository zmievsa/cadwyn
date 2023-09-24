from fastapi.routing import APIRoute


class UniversiError(Exception):
    pass


class LintingError(UniversiError):
    pass


class CodeGenerationError(UniversiError):
    pass


class InvalidGenerationInstructionError(CodeGenerationError):
    pass


class RouterGenerationError(UniversiError):
    pass


class RouteAlreadyExistsError(RouterGenerationError):
    def __init__(self, *routes: APIRoute):
        self.routes = routes
        super().__init__(f"The following routes are duplicates of each other: {routes}")


class UniversiStructureError(UniversiError):
    pass


class ModuleIsNotVersionedError(ValueError):
    pass
