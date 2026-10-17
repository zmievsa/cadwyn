"""A model whose `Annotated` field refers to a model that is defined after it, so pydantic resolves it later"""

from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, Field


def validate_child(child: object) -> object:
    return child


class Parent(BaseModel):
    child: Annotated[Optional["Child"], AfterValidator(validate_child)] = Field(default=None, validate_default=True)


class Child(BaseModel):
    x: int = 0
