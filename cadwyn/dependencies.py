from cadwyn._internal.context_vars import CURRENT_DEPENDENCY_SOLVER_OPTIONS, CURRENT_DEPENDENCY_SOLVER_VAR


async def current_dependency_solver() -> CURRENT_DEPENDENCY_SOLVER_OPTIONS:
    return CURRENT_DEPENDENCY_SOLVER_VAR.get("fastapi")
