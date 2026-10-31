from typing import Annotated, Literal

from cadwyn import current_dependency_solver
from fastapi import Depends


def my_dependency(
    dependency_solver: Annotated[
        Literal["fastapi", "cadwyn"], Depends(current_dependency_solver)
    ],
):
    if dependency_solver == "fastapi":  # Before migration
        ...
    else:  # After migration
        ...
