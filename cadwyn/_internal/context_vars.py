from contextvars import ContextVar

from typing_extensions import Literal

DEFAULT_API_VERSION_VAR: "ContextVar[str | None]" = ContextVar("cadwyn_default_api_version")
CURRENT_DEPENDENCY_SOLVER_OPTIONS = Literal["cadwyn", "fastapi"]
CURRENT_DEPENDENCY_SOLVER_VAR: ContextVar[CURRENT_DEPENDENCY_SOLVER_OPTIONS] = ContextVar(
    "cadwyn_dependencies_current_dependency_solver"
)
