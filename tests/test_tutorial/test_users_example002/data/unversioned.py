from pydantic import Field

from cadwyn import internal_body_representation_of

from .latest.users import UserCreateRequest


@internal_body_representation_of(UserCreateRequest)
class InternalUserCreateRequest(UserCreateRequest):
    addresses_to_create: list[str] = Field(default_factory=list)
