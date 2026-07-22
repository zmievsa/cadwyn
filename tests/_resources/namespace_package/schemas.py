# ruff: noqa: INP001

from pydantic import BaseModel, Field


class NamespacePackageSchema(BaseModel):
    value: str = Field(coerce_numbers_to_str=True)
