from fastapi import HTTPException
from src.versions import RejectUserAddressesMissingFromExternalService

ADDRESSES_KNOWN_TO_THE_EXTERNAL_SERVICE = {"1 Main St"}


def check_user_address_exists_in_an_external_service(address: str) -> None:
    if address not in ADDRESSES_KNOWN_TO_THE_EXTERNAL_SERVICE:
        raise HTTPException(400, "The address does not exist")


async def create_user(payload):
    if RejectUserAddressesMissingFromExternalService.is_applied:
        check_user_address_exists_in_an_external_service(payload.address)
    ...
