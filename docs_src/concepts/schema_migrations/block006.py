from cadwyn import VersionChange, schema
from pydantic import BaseModel


class Invoice(BaseModel):
    code: str


class BusinessInvoice(Invoice):
    pass


class PersonalInvoice(Invoice):
    pass


class AllowLongBusinessInvoiceCodes(VersionChange):
    """Business invoice codes can now be longer than 5 characters."""

    instructions_to_migrate_to_previous_version = (
        schema(BusinessInvoice).field("code").had(max_length=5),
    )
