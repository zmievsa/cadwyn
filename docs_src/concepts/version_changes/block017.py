from typing import Annotated, Literal

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionedAPIRouter,
    current_dependency_solver,
)
from fastapi import BackgroundTasks, Depends

sent_emails: list[str] = []


def send_welcome_email(email: str) -> None:
    sent_emails.append(email)


def schedule_welcome_email(
    email: str,
    background_tasks: BackgroundTasks,
    dependency_solver: Annotated[
        Literal["fastapi", "cadwyn"], Depends(current_dependency_solver)
    ],
):
    # Dependencies run twice, so we add the task in only one of the runs
    if dependency_solver == "fastapi":
        background_tasks.add_task(send_welcome_email, email)


router = VersionedAPIRouter()


@router.post("/users", dependencies=[Depends(schedule_welcome_email)])
async def create_user():
    return {}


app = Cadwyn(
    versions=VersionBundle(Version("2023-02-10"), Version("2022-11-16")),
    run_background_tasks_of_latest_version_dependencies=True,
)
app.generate_and_include_versioned_routers(router)
