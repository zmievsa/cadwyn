from pydantic import BaseModel


class BaseCompany(BaseModel):
    name: str


class CompanyCreateRequest(BaseCompany):
    default_vat_id: str


class CompanyResource(BaseCompany):
    pass


class CompanyVATIDResource(BaseModel):
    id: int
    value: str


class CompanyVATIDResourceList(BaseModel):
    data: list[CompanyVATIDResource]
