from uuid import UUID

from pydantic import BaseModel


class BaseCompany(BaseModel):
    name: str
    vat_id: str


class CompanyCreateRequest(BaseCompany):
    pass


class CompanyResource(BaseCompany):
    id: UUID


class CompanyVATIDResource(BaseModel):
    id: UUID
    value: str


class CompanyVATIDResourceList(BaseModel):
    data: list[CompanyVATIDResource]
