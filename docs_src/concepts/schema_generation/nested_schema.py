from pydantic import BaseModel

from cadwyn import (
    Cadwyn,
    Version,
    VersionBundle,
    VersionChange,
    VersionedAPIRouter,
    schema,
)
from docs_src.concepts.schema_generation.external_wrapper import Page


class Customer(BaseModel):
    name: str
    address: str


class AddCustomerAddress(VersionChange):
    description = "Add an address to customer responses"
    instructions_to_migrate_to_previous_version = (
        schema(Customer).field("address").didnt_exist,
    )


router = VersionedAPIRouter()


@router.get("/customers", response_model=Page[Customer])
async def list_customers() -> dict[str, object]:
    return {
        "items": [
            {
                "name": "John Doe",
                "address": "123 Cherry Lane",
            }
        ],
        "total": 1,
    }


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", AddCustomerAddress),
        Version("2000-01-01"),
    )
)
app.generate_and_include_versioned_routers(router)
