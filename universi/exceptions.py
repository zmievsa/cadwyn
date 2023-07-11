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


class UniversiStructureError(UniversiError):
    pass
