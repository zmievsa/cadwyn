from datetime import datetime

from pydantic import BaseModel


class BaseInvoice(BaseModel):
    created_at: datetime


class InvoiceCreateRequest(BaseInvoice):
    pass


class InvoiceResource(BaseInvoice):
    id: int
