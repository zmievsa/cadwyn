from typing import Any
from pydantic import BaseModel

from universi.fields import FillablePrivateAttr, FillablePrivateAttrMixin


class UserCreateRequest(FillablePrivateAttrMixin, BaseModel):
    default_address: str
    _addresses_to_create: list[str] = FillablePrivateAttr(default_factory=list)


class UserResource(BaseModel):
    id: int


class UserAddressResource(BaseModel):
    id: int
    value: str


class UserAddressResourceList(BaseModel):
    data: list[UserAddressResource]
