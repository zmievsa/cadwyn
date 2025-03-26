from cadwyn.structure.versions import _CURRENT_DEPENDENCY_SOLVER_OPTIONS, _CURRENT_DEPENDENCY_SOLVER_VAR


async def current_dependency_solver() -> _CURRENT_DEPENDENCY_SOLVER_OPTIONS:
    return _CURRENT_DEPENDENCY_SOLVER_VAR.get("fastapi")
