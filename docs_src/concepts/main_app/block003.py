from typing import Annotated

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)
from fastapi import Depends
from fastapi_pagination import Page, Params, paginate

from docs_src.concepts.main_app.users import User, get_users


class AddUserName(VersionChange):
    description = "Users now have names"
    instructions_to_migrate_to_previous_version = (
        schema(User).field("name").didnt_exist,
    )


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AddUserName),
        Version("2000-01-01"),
    )
)
router = VersionedAPIRouter()


@router.get("/users")
async def list_users(params: Annotated[Params, Depends()]) -> Page[User]:
    return paginate(get_users(), params)


app.generate_and_include_versioned_routers(router)
