from typing import Annotated

from cadwyn import VersionedAPIRouter
from fastapi import Header
from fastapi.params import Param

router = VersionedAPIRouter()


@router.only_exists_in_older_versions
@router.get("/users")
def get_users_by_name_before_starting_to_use_params(
    user_name: Annotated[str, Header()],
):
    """Some code that references user_name"""


@router.get("/users")
def get_users_by_name(user_name: Annotated[str, Param()]):
    """Some code that references user_name"""
