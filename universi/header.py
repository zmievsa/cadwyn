import datetime
import types
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from fastapi import Depends, Header

api_version_var: ContextVar[datetime.date | None] = ContextVar(
    "api_version",
    default=None,
)


def get_universi_dependency(
    *,
    version_header_name: str,
    default_version: datetime.date | None = None,
    extra_kwargs_to_header_constructor: Mapping[str, Any] = types.MappingProxyType({}),
):
    if default_version is None:
        extra_kwargs: Mapping[str, Any] = extra_kwargs_to_header_constructor
    else:
        extra_kwargs = extra_kwargs_to_header_constructor | {"default": default_version}

    async def dependency(
        api_version: datetime.date = Header(alias=version_header_name, **extra_kwargs),
    ):
        api_version_var.set(api_version)
        return api_version

    return Depends(dependency)
